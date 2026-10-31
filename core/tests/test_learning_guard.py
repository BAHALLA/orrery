"""The learning sandbox's answer guard and its matcher (AEP-032 §4)."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.sessions import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from orrery_core import learning_gate
from orrery_core.learning.guard import AnswerGuardPlugin, number_words, states_value
from orrery_core.learning.manifest import ManifestStore
from orrery_core.learning.runtime import Runtime
from orrery_core.serving.gateway import AgentGateway, InboundMessage

# ── Matcher ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "The lag is 5100 messages.",
        "lag: 5,100",
        "lag: 5.100",
        "lag: 5 100",
        "lag: 5 100",
        "lag: 5'100",
        "lag: 5_100",
        "lag=5100msgs",
        "(5100)",
        "about 5.1k",
        "about 5,1 K messages",
        "roughly 5k",
        "five thousand one hundred",
        "Five-thousand one hundred messages",
        "five thousand and one hundred",
        "about 5.1 thousand",
        "about 5.1 thousands",
        "5 thousand messages",
        "lag: 5100.0",
        "lag: 5,100.00",
    ],
)
def test_states_an_integer_in_the_forms_people_write(text):
    assert states_value(text, 5100)


@pytest.mark.parametrize(
    "text",
    [
        "The lag is 25100.",
        "The lag is 51000.",
        "version v5100",
        "5100.5 seconds",
        "1,5100",
        "about 7k",
        "five thousand",
        "fifty-one hundred",  # an approximation the matcher does not claim
        "check the 5 k8s nodes",
        "twenty five thousand one hundred",
        "five thousand one hundred twenty",
        "5100.05",
        "5,100,000 bytes",
        "5.100.000",
        "",
    ],
)
def test_does_not_flag_near_misses(text):
    assert not states_value(text, 5100)


def test_larger_numbers_need_a_consistent_separator():
    assert states_value("1,234,567 bytes", 1234567)
    assert states_value("1 234 567 bytes", 1234567)
    assert not states_value("1,234 567 bytes", 1234567)
    assert states_value("about 1.2M", 1234567)
    assert states_value("1.23 million", 1234567)
    assert not states_value("about 1M", 1234567)  # too far to be the answer
    assert not states_value("1.3M", 1234567)  # not its rounding


def test_decimals_match_with_either_mark():
    assert states_value("p99 is 412.5 ms", 412.5)
    assert states_value("p99 is 412,5 ms", 412.5)
    assert states_value("p99 is 412.50 ms", 412.5)
    assert states_value("total 1,234.50", 1234.5)
    assert not states_value("p99 is 412.55 ms", 412.5)
    assert not states_value("p99 is 1412.5 ms", 412.5)


def test_strings_match_case_insensitively():
    assert states_value("The culprit is ORDERS-PROCESSOR.", "orders-processor")
    assert not states_value("The culprit is orders.", "orders-processor")


def test_number_words():
    assert number_words(5100) == "five thousand one hundred"
    assert number_words(1_000_042) == "one million forty two"
    assert number_words(999) == "nine hundred ninety nine"


# ── Plugin ───────────────────────────────────────────────────────────

_MANIFEST = {
    "schema_version": 1,
    "scenario_id": "kafka-lag-01",
    "seeded_at": "2026-10-30T10:00:00Z",
    "default_disclosure": 0,
    "targets": [
        {
            "id": "lag",
            "descriptor": "the orders-processor group's total lag",
            "value": 5100,
            "unit": "messages",
        },
        {"id": "pod", "descriptor": "the crashing worker", "value": "worker-7f9c", "unit": ""},
    ],
}


@pytest.fixture
def store(tmp_path: Path) -> ManifestStore:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_MANIFEST))
    return ManifestStore(path)


def _response(*parts: types.Part, partial: bool | None = None) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=list(parts)), partial=partial)


def _parts(response: LlmResponse) -> list[types.Part]:
    assert response.content and response.content.parts
    return response.content.parts


def _first_text(response: LlmResponse) -> str:
    return _parts(response)[0].text or ""


def _plugin(store: ManifestStore, level: int = 0) -> AnswerGuardPlugin:
    return AnswerGuardPlugin(
        store=store, root_agent_name=lambda: "root", level_of=lambda _ctx: level
    )


def _ctx(agent: str = "root") -> MagicMock:
    ctx = MagicMock()
    ctx.agent_name = agent
    return ctx


@pytest.mark.asyncio
async def test_rewrites_the_roots_answer_in_place(store):
    response = _response(types.Part.from_text(text="Total lag is 5,100 messages."))

    with patch("orrery_core.learning.guard.audit_event") as audit:
        result = await _plugin(store).after_model_callback(
            callback_context=_ctx(), llm_response=response
        )

    # None keeps the after-model chain going (TokenBudgetPlugin settles there).
    assert result is None
    [part] = _parts(response)
    text = part.text or ""
    assert "5,100" not in text and not states_value(text, 5100)
    assert "orders-processor group's total lag" in text
    audit.assert_called_once()
    assert audit.call_args.args == ("learning_answer_withheld",)
    fields = audit.call_args.kwargs
    assert fields["target"] == "lag" and fields["scenario_id"] == "kafka-lag-01"
    # The value itself is never audited.
    assert "5100" not in json.dumps(fields) and "5,100" not in json.dumps(fields)


@pytest.mark.asyncio
async def test_keeps_tool_calls_and_drops_thoughts(store):
    call = types.Part.from_function_call(name="kafka_agent", args={"request": "lag?"})
    thought = types.Part(text="the lag is 5100", thought=True)
    response = _response(thought, types.Part.from_text(text="It's 5100. Checking more."), call)

    await _plugin(store).after_model_callback(callback_context=_ctx(), llm_response=response)

    parts = _parts(response)
    assert [p.function_call is not None for p in parts] == [False, True]
    assert all(p.thought is not True for p in parts)
    assert "5100" not in _first_text(response)


@pytest.mark.asyncio
async def test_a_string_target_is_withheld_too(store):
    response = _response(types.Part.from_text(text="The pod is worker-7f9c."))
    await _plugin(store).after_model_callback(callback_context=_ctx(), llm_response=response)
    assert "worker-7f9c" not in _first_text(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "partial", "level", "text"),
    [
        ("kafka_agent", None, 0, "lag is 5100"),  # a specialist: the root re-states it
        ("root", True, 0, "lag is 5100"),  # partials are held, not shown
        ("root", None, 3, "lag is 5100"),  # revealed
        ("root", None, 0, "lag is high, look at the group"),  # nothing stated
    ],
)
async def test_leaves_alone(store, agent, partial, level, text):
    response = _response(types.Part.from_text(text=text), partial=partial)
    await _plugin(store, level).after_model_callback(
        callback_context=_ctx(agent), llm_response=response
    )
    assert _first_text(response) == text


@pytest.mark.asyncio
async def test_falls_back_when_the_nudge_would_state_a_value(tmp_path):
    manifest = {
        **_MANIFEST,
        # A descriptor that spells another target's value as an abbreviation.
        "targets": [
            {"id": "a", "descriptor": "the 5k backlog", "value": 4990, "unit": ""},
            {"id": "b", "descriptor": "the lag", "value": 777, "unit": ""},
        ],
    }
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest))
    response = _response(types.Part.from_text(text="Group lag is 4990."))

    await _plugin(ManifestStore(path)).after_model_callback(
        callback_context=_ctx(), llm_response=response
    )

    text = _first_text(response)
    assert not states_value(text, 4990) and "5k" not in text


@pytest.mark.asyncio
async def test_one_nudge_per_turn(store):
    plugin = _plugin(store)
    ctx = _ctx()
    ctx.invocation_id = "inv-1"
    first = _response(types.Part.from_text(text="lag is 5100"))
    second = _response(types.Part.from_text(text="again: 5100"))

    call = types.Part.from_function_call(name="kafka_agent", args={"request": "more"})
    with_call = _response(types.Part.from_text(text="it was 5100, checking"), call)

    await plugin.after_model_callback(callback_context=ctx, llm_response=first)
    await plugin.after_model_callback(callback_context=ctx, llm_response=with_call)
    await plugin.after_model_callback(callback_context=ctx, llm_response=second)

    assert "can't give that value" in _first_text(first)
    # No empty text part, ever: the call alone, or a short fixed line.
    assert [p.function_call is not None for p in _parts(with_call)] == [True]
    assert _first_text(second).strip() and "can't give" not in _first_text(second)
    assert not states_value(_first_text(second), 5100)
    ctx.invocation_id = "inv-2"
    third = _response(types.Part.from_text(text="5100"))
    await plugin.after_model_callback(callback_context=ctx, llm_response=third)
    assert "can't give that value" in _first_text(third)


# ── Through a real gateway ───────────────────────────────────────────


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    script: list[Any] = []

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        turn = self.script.pop(0)
        for response in turn if isinstance(turn, list) else [turn]:
            yield response


def _text(text: str, *, partial: bool | None = None) -> LlmResponse:
    return _response(types.Part.from_text(text=text), partial=partial)


def _call(name: str, **args: Any) -> LlmResponse:
    return _response(types.Part.from_function_call(name=name, args=args))


async def get_consumer_lag() -> dict:
    """Read the group's lag."""
    return {"status": "success", "total_lag": 5100}


@pytest.fixture
def active(store, monkeypatch) -> Runtime:
    runtime = Runtime(store=store)
    monkeypatch.setattr(learning_gate, "_runtime", runtime)
    return runtime


@pytest.mark.asyncio
async def test_the_learner_never_reads_the_value_through_a_specialist(active):
    specialist = LlmAgent(
        name="kafka_agent",
        description="Kafka checks",
        model=ScriptedLlm(script=[_call("get_consumer_lag"), _text("total lag is 5100")]),
        tools=[get_consumer_lag],
    )
    root = LlmAgent(
        name="root",
        model=ScriptedLlm(
            script=[
                _call("kafka_agent", request="lag?"),
                [
                    _text("The lag is 5", partial=True),
                    _text("100.", partial=True),
                    _text("The lag is 5100."),
                ],
            ]
        ),
        tools=[AgentTool(agent=specialist)],
    )
    sessions = InMemorySessionService()
    gateway = AgentGateway(app_name="t", root_agent=root, session_service=sessions)
    partials: list[str] = []

    async def on_event(event: Any) -> None:
        if event.partial is True:
            partials.append(event.content.parts[0].text)

    reply = await gateway.run(
        InboundMessage(text="what's the lag?", user_id="u", conversation_key="", channel="t"),
        on_event=on_event,
        streaming=True,
    )

    assert not states_value(reply.text, 5100)
    # The stored transcript holds the nudge too, so a reopened chat shows the same.
    session = await sessions.get_session(app_name="t", user_id="u", session_id=reply.session_id)
    assert session is not None
    root_texts = [
        p.text
        for e in session.events
        if e.author == "root" and e.content
        for p in e.content.parts or []
        if p.text
    ]
    assert root_texts and not any(states_value(t, 5100) for t in root_texts)
    # Partials still reach the gateway's callback: the SSE layer is what holds them.
    assert partials


@pytest.mark.asyncio
async def test_a_normal_gateway_carries_no_guard():
    root = LlmAgent(name="root", model=ScriptedLlm(script=[_text("lag is 5100")]))
    gateway = AgentGateway(app_name="t", root_agent=root, session_service=InMemorySessionService())
    assert not any(isinstance(p, AnswerGuardPlugin) for p in gateway.runner.app.plugins)
    reply = await gateway.run(
        InboundMessage(text="lag?", user_id="u", conversation_key="", channel="t")
    )
    assert reply.text == "lag is 5100"


def test_the_guard_follows_a_replaced_root(active):
    first = LlmAgent(name="first", model=ScriptedLlm())
    gateway = AgentGateway(app_name="t", root_agent=first, session_service=InMemorySessionService())
    gateway.replace_root_agent(LlmAgent(name="second", model=ScriptedLlm()))
    [guard] = [p for p in gateway.runner.app.plugins if isinstance(p, AnswerGuardPlugin)]
    assert guard._root() == "second"


def test_the_guard_runs_first_among_after_model_plugins(active):
    from orrery_core import default_plugins

    root = LlmAgent(name="root", model=ScriptedLlm())
    gateway = AgentGateway(
        app_name="t",
        root_agent=root,
        plugins=default_plugins(daily_token_budget=1000),
        session_service=InMemorySessionService(),
    )
    after_model = [
        p
        for p in gateway.runner.app.plugins
        if type(p).after_model_callback is not BasePlugin.after_model_callback
    ]
    assert isinstance(after_model[0], AnswerGuardPlugin)


#: Plugins with a before-model hook, each reviewed: an answer from there skips
#: the guard (AEP-032 §4). TokenBudgetPlugin answers with a fixed text; the
#: other two only edit the request and return None.
_REVIEWED_BEFORE_MODEL = {"TokenBudgetPlugin", "CallBudgetPlugin", "AttachmentPlugin"}


def test_no_unreviewed_plugin_can_answer_before_the_model(active):
    from orrery_core import default_plugins

    gateway = AgentGateway(
        app_name="t",
        root_agent=LlmAgent(name="root", model=ScriptedLlm()),
        plugins=default_plugins(
            enable_auth=True, enable_memory=False, daily_token_budget=1000, enable_tracing=False
        ),
        session_service=InMemorySessionService(),
    )
    answerers = {
        type(p).__name__
        for p in gateway.runner.app.plugins
        if type(p).before_model_callback is not BasePlugin.before_model_callback
    }
    assert answerers <= _REVIEWED_BEFORE_MODEL, answerers - _REVIEWED_BEFORE_MODEL


def test_the_token_budget_refusal_is_fixed_text():
    from orrery_core.plugins import token_budget_plugin as tb

    for message in (tb._USER_MESSAGE, tb._GLOBAL_MESSAGE):
        assert isinstance(message, str) and "{" not in message
