"""One console thread is one session, across turns and across catalog edits.

End to end through the HTTP API with a real Runner and session store: the only
stand-in is the model, which answers with how many user messages it was shown.
That count is the observable: it only grows if the turns share one session.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types

from orrery_core.agent.catalog_store import AgentCatalog, InMemoryCatalogStore
from orrery_core.security.auth import JWTConfig
from orrery_core.serving.agent_admin import AdminOptions, SpecialistOption, ToolOption
from orrery_core.serving.server import ServerConfig, create_app

SECRET = "s" * 40


class _CountingLlm(BaseLlm):
    """Replies with the user messages in the request it was given."""

    model: str = "counting"

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        said = [
            part.text
            for content in llm_request.contents
            if content.role == "user"
            for part in content.parts or []
            if part.text
        ]
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part.from_text(text="|".join(said))])
        )


def _root() -> LlmAgent:
    return LlmAgent(name="root", model=_CountingLlm(), instruction="Answer.")


class _Admin:
    """Rebuilds the same tree on every edit, as the real one does."""

    builds = 0

    def options(self) -> AdminOptions:
        return AdminOptions(
            specialists=[SpecialistOption("k8s", "k8s_a", "Kubernetes", "builtin")],
            tools=[ToolOption("t", "k8s", "read")],
        )

    def build(self, catalog: AgentCatalog) -> Any:
        self.builds += 1
        return _root()


def _client() -> TestClient:
    with patch(
        "orrery_core.serving.server.create_session_service",
        return_value=InMemorySessionService(),
    ):
        app = create_app(
            root_agent=_root(),
            app_name="t",
            plugins=[],
            config=ServerConfig(
                auth_enabled=True,
                jwt=JWTConfig(algorithm="HS256", secret=SECRET),
                admin_editing=True,
                catalog_poll_seconds=0,
            ),
            agent_admin=_Admin(),
            catalog_store=InMemoryCatalogStore(),
        )
    return TestClient(app)


def _bearer(sub: str, role: str = "admin") -> dict:
    token = jwt.encode(
        {"sub": sub, "roles": [role], "exp": time.time() + 60}, SECRET, algorithm="HS256"
    )
    return {"Authorization": f"Bearer {token}"}


def _say(client: TestClient, headers: dict, text: str, session_id: str | None = None) -> dict:
    res = client.post("/chat", headers=headers, json={"message": text, "session_id": session_id})
    assert res.status_code == 200, res.text
    return res.json()


def _say_streamed(client: TestClient, headers: dict, text: str, session_id: str) -> str:
    res = client.post(
        "/chat/stream", headers=headers, json={"message": text, "session_id": session_id}
    )
    assert res.status_code == 200, res.text
    sessions = [
        json.loads(line[len("data: ") :])["session_id"]
        for line in res.text.splitlines()
        if line.startswith("data: ") and '"session_id"' in line
    ]
    assert sessions, res.text
    return sessions[0]


def test_a_thread_is_one_session_across_turns_and_catalog_edits():
    me = _bearer(f"u-{uuid.uuid4().hex[:6]}")
    with _client() as client:
        first = _say(client, me, "one")
        sid = first["session_id"]
        assert first["response"] == "one"

        second = _say(client, me, "two", sid)
        assert second["session_id"] == sid
        assert second["response"] == "one|two"  # the model saw the whole thread

        # An admin saves the catalog: the coordinator is rebuilt. The thread
        # must carry on in the same session, history intact.
        put = client.put(
            "/admin/catalog", headers=me, json={"version": 0, "disabled_specialists": []}
        )
        assert put.status_code == 200, put.text

        third = _say(client, me, "three", sid)
        assert third["session_id"] == sid
        assert third["response"] == "one|two|three"

        # The console's streamed path keeps the thread too.
        assert _say_streamed(client, me, "four", sid) == sid
        assert _say(client, me, "five", sid)["response"] == "one|two|three|four|five"

        listed = client.get("/sessions", headers=me).json()["sessions"]
        ids = [s["session_id"] for s in listed]
        assert ids == [sid]  # five turns, one session


def test_new_chat_is_a_new_session_and_threads_stay_apart():
    me = _bearer(f"u-{uuid.uuid4().hex[:6]}")
    with _client() as client:
        a = _say(client, me, "alpha")["session_id"]
        b = _say(client, me, "beta")  # no session id: "New chat"
        assert b["session_id"] != a and b["response"] == "beta"
        assert _say(client, me, "again", a)["response"] == "alpha|again"


def test_another_users_session_id_starts_a_fresh_session():
    alice, bob = _bearer("alice"), _bearer("bob")
    with _client() as client:
        sid = _say(client, alice, "secret plan")["session_id"]
        reply = _say(client, bob, "what did she say?", sid)
        assert reply["session_id"] != sid
        assert reply["response"] == "what did she say?"  # nothing of alice's thread
