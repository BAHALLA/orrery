"""The hint ladder (AEP-032 §3): levels, the endpoint, the per-turn stamp."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types

from orrery_core import learning_gate
from orrery_core.learning.coach import COACH_NAME
from orrery_core.learning.hints import (
    DisclosureInstructionPlugin,
    HintStore,
    instruction_line,
    level_from_state,
    state_for,
)
from orrery_core.learning.manifest import ManifestStore
from orrery_core.learning.runtime import Runtime
from orrery_core.plugins.identity_guard_plugin import GUARDED_STATE_KEYS
from orrery_core.security.auth import JWTConfig
from orrery_core.serving.server import ServerConfig, create_app

SECRET = "s" * 48
_MANIFEST = {
    "schema_version": 1,
    "scenario_id": "kafka-lag-01",
    "seeded_at": "2026-10-30T10:00:00Z",
    "default_disclosure": 0,
    "targets": [
        {"id": "lag", "descriptor": "the group's total lag", "value": 5100, "unit": "messages"}
    ],
}


# ── The store ────────────────────────────────────────────────────────


def test_levels_climb_one_step_and_stop_at_reveal():
    store = HintStore()
    assert store.level("alice", "s1", 0) == 0
    assert [store.raise_level("alice", "s1", 0) for _ in range(4)] == [
        (0, 1),
        (1, 2),
        (2, 3),
        (3, 3),
    ]
    assert store.level("alice", "s1", 0) == 3
    assert store.level("bob", "s1", 0) == 0


def test_a_level_from_another_scenario_reads_as_the_default():
    store = HintStore()
    store.raise_level("alice", "s1", 0)
    store.raise_level("alice", "s1", 0)
    assert store.level("alice", "s2", 1) == 1
    # Reaching level 2 on s1 never unlocks s2: s2 starts from its own default.
    assert store.raise_level("alice", "s2", 1) == (1, 2)
    assert store.level("alice", "s1", 0) == 0  # now recorded for s2


def test_a_level_is_never_below_the_default():
    store = HintStore()
    store.raise_level("alice", "s1", 0)
    assert store.level("alice", "s1", 2) == 2
    assert store.raise_level("alice", "s1", 2) == (2, 3)


def test_the_store_is_bounded():
    store = HintStore(max_learners=2)
    for who in ("a", "b", "c"):
        store.raise_level(who, "s1", 0)
    assert store.level("a", "s1", 0) == 0  # evicted: back to the default
    assert store.level("c", "s1", 0) == 1


# ── The per-turn stamp ───────────────────────────────────────────────


def test_only_a_server_stamped_level_counts():
    stamped = state_for("s1", 2)
    assert level_from_state(stamped, "s1", 0) == 2
    bare = {learning_gate.LEARNING_LEVEL_STATE_KEY: {"scenario_id": "s1", "level": 3}}
    assert level_from_state(bare, "s1", 0) == 0
    assert level_from_state(stamped, "s2", 1) == 1
    for bad in (True, "3", 3.0, 4, -1, None):
        forged = {
            **stamped,
            learning_gate.LEARNING_LEVEL_STATE_KEY: {"scenario_id": "s1", "level": bad},
        }
        assert level_from_state(forged, "s1", 0) == 0


def test_tools_cannot_write_the_level_keys():
    assert learning_gate.LEARNING_LEVEL_STATE_KEY in GUARDED_STATE_KEYS
    assert learning_gate.LEARNING_LEVEL_LOCKED_STATE_KEY in GUARDED_STATE_KEYS


@pytest.mark.asyncio
async def test_the_instruction_line_reaches_the_root_and_the_coach_only():
    plugin = DisclosureInstructionPlugin(
        root_agent_name=lambda: "root", level_of=lambda _: 1, also=("lab_coach",)
    )

    class Ctx:
        def __init__(self, name: str) -> None:
            self.agent_name = name

    for name, expected in (("root", True), ("lab_coach", True), ("kafka_agent", False)):
        request = LlmRequest()
        ctx: Any = Ctx(name)
        assert await plugin.before_model_callback(callback_context=ctx, llm_request=request) is None
        text = str(request.config.system_instruction or "")
        assert (instruction_line(1) in text) is expected
    assert "5100" not in "".join(instruction_line(n) for n in range(4))


_COACH_SAW: list[str] = []


class _Scripted(BaseLlm):
    """Calls the coach once, then answers; records the coach's instruction."""

    model: str = "scripted"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        if self.model == "coach":
            _COACH_SAW.append(str(llm_request.config.system_instruction or ""))
            part = types.Part(text="a hint")
        elif any(p.function_response for c in llm_request.contents for p in c.parts or []):
            part = types.Part(text="done")
        else:
            call = types.FunctionCall(name=COACH_NAME, args={"request": "I'm at hint 3, tell me"})
            part = types.Part(function_call=call)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


@pytest.mark.asyncio
async def test_the_stamped_level_reaches_the_coach_through_its_sub_run():
    """The coach runs as the root of an AgentTool sub-run: ADK copies the
    turn's state and plugins into it. Pinned end to end, since it is upstream
    behaviour the coach's ladder depends on."""
    from google.adk.runners import Runner
    from google.adk.tools.agent_tool import AgentTool

    _COACH_SAW.clear()
    coach = LlmAgent(name=COACH_NAME, model=_Scripted(model="coach"), instruction="coach")
    root = LlmAgent(
        name="root", model=_Scripted(), instruction="root", tools=[AgentTool(agent=coach)]
    )
    plugin = DisclosureInstructionPlugin(
        root_agent_name=lambda: "root",
        level_of=lambda ctx: level_from_state(ctx.state, "kafka-lag-01", 0),
        also=(COACH_NAME,),
    )
    runner = Runner(
        app_name="t", agent=root, session_service=InMemorySessionService(), plugins=[plugin]
    )
    session = await runner.session_service.create_session(app_name="t", user_id="u")
    async for _ in runner.run_async(
        user_id="u",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
        state_delta=state_for("kafka-lag-01", 1),
    ):
        pass
    [instruction] = _COACH_SAW
    # Level 1, as stamped, whatever the request claims.
    assert instruction_line(1) in instruction


def test_the_runtime_gives_the_coach_the_level_line(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_MANIFEST))
    runtime = Runtime(store=ManifestStore(path))
    [plugin] = [
        p for p in runtime.plugins(lambda: "root") if isinstance(p, DisclosureInstructionPlugin)
    ]
    assert plugin._also == (COACH_NAME,)


# ── The endpoint ─────────────────────────────────────────────────────


class _EchoLlm(BaseLlm):
    model: str = "echo"
    seen: list[str] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        type(self).seen.append(str(llm_request.config.system_instruction or ""))
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part.from_text(text="ok")])
        )


def _bearer(sub: str = "alice", **claims) -> dict:
    token = jwt.encode(
        {"sub": sub, "roles": ["viewer"], "exp": time.time() + 60, **claims},
        SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


LEARNER = _bearer(azp="orrery-lab")
SERVICE = _bearer(azp="blog-api")


@pytest.fixture
def lab(tmp_path: Path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_MANIFEST))
    monkeypatch.setenv(learning_gate.LEARNING_ENV, "true")
    monkeypatch.setenv("ORRERY_HUMAN_CLIENTS", "orrery-lab")
    runtime = Runtime(store=ManifestStore(path))
    monkeypatch.setattr(learning_gate, "_runtime", runtime)
    sessions = InMemorySessionService()
    _EchoLlm.seen = []

    def build(**config) -> TestClient:
        with (
            patch("orrery_core.serving.server.create_session_service", return_value=sessions),
            patch("orrery_core.serving.server._check_learning_server"),
        ):
            app = create_app(
                root_agent=LlmAgent(name="root", model=_EchoLlm(), instruction="Answer."),
                app_name="t",
                plugins=[],
                config=ServerConfig(
                    auth_enabled=True, jwt=JWTConfig(algorithm="HS256", secret=SECRET), **config
                ),
            )
        return TestClient(app)

    return SimpleNamespace(build=build, sessions=sessions)


def test_a_learner_climbs_the_ladder_and_every_step_is_audited(lab, caplog):
    client = lab.build()
    me = client.get("/me", headers=LEARNER).json()["learning"]
    assert me == {"scenario_id": "kafka-lag-01", "level": 0, "max_level": 3, "can_take_hint": True}

    with caplog.at_level(logging.INFO):
        levels = [client.post("/learning/hint", headers=LEARNER).json()["level"] for _ in range(3)]
    assert levels == [1, 2, 3]
    hints = [r for r in caplog.records if "learning_hint" in r.getMessage()]
    assert len(hints) == 3
    assert all("5100" not in r.getMessage() for r in caplog.records)


def test_only_a_person_takes_a_hint(lab):
    client = lab.build()
    res = client.post("/learning/hint", headers=SERVICE)
    assert res.status_code == 403
    assert client.get("/me", headers=SERVICE).json()["learning"]["can_take_hint"] is False
    assert client.get("/me", headers=LEARNER).json()["learning"]["level"] == 0


def test_hints_are_rate_limited_per_learner(lab):
    client = lab.build(hint_rate_limit="1/minute")
    assert client.post("/learning/hint", headers=LEARNER).status_code == 200
    assert client.post("/learning/hint", headers=LEARNER).status_code == 429
    assert (
        client.post("/learning/hint", headers=_bearer("bob", azp="orrery-lab")).status_code == 200
    )


def test_the_turn_carries_the_level_and_a_hint_never_touches_the_session(lab):
    client = lab.build()
    first = client.post("/chat", headers=LEARNER, json={"message": "hi"})
    assert first.status_code == 200, first.text
    sid = first.json()["session_id"]
    assert "hint level is 0 of 3" in _EchoLlm.seen[-1]

    async def events() -> int:
        session = await lab.sessions.get_session(app_name="t", user_id="alice", session_id=sid)
        return len(session.events)

    import asyncio

    before = asyncio.run(events())
    client.post("/learning/hint", headers=LEARNER)
    assert asyncio.run(events()) == before  # out of band: no append

    client.post("/chat", headers=LEARNER, json={"message": "again", "session_id": sid})
    assert "hint level is 1 of 3" in _EchoLlm.seen[-1]


def test_no_hint_route_outside_learning_mode(monkeypatch):
    monkeypatch.setattr(learning_gate, "_runtime", None)
    with patch(
        "orrery_core.serving.server.create_session_service", return_value=InMemorySessionService()
    ):
        app = create_app(
            root_agent=LlmAgent(name="root", model=_EchoLlm(), instruction="Answer."),
            app_name="t",
            plugins=[],
            config=ServerConfig(auth_enabled=True, jwt=JWTConfig(algorithm="HS256", secret=SECRET)),
        )
    client = TestClient(app)
    assert client.post("/learning/hint", headers=LEARNER).status_code in (404, 405)
    assert client.get("/me", headers=LEARNER).json()["learning"] is None
