"""A machine principal on the console sees only the sessions minted for it.

ADK keys sessions by ``user_id``, and a delegated agent's ``user_id`` is its
user's subject, so subject scoping alone let any agent acting for alice list,
read and continue alice's conversations and every other agent's. End to end
through the HTTP API with a real Runner and session store; the model echoes
the user messages it was shown, which is how a test sees whose thread it is in.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types

from orrery_core.security.auth import JWTConfig
from orrery_core.serving.server import ServerConfig, create_app

SECRET = "s" * 40


class _EchoLlm(BaseLlm):
    """Replies with every user message in the request it was given."""

    model: str = "echo"

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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORRERY_HUMAN_CLIENTS", "orrery-console")
    with patch(
        "orrery_core.serving.server.create_session_service",
        return_value=InMemorySessionService(),
    ):
        app = create_app(
            root_agent=LlmAgent(name="root", model=_EchoLlm(), instruction="Answer."),
            app_name="t",
            plugins=[],
            config=ServerConfig(auth_enabled=True, jwt=JWTConfig(algorithm="HS256", secret=SECRET)),
        )
    with TestClient(app) as test_client:
        yield test_client


def _bearer(claims: dict) -> dict:
    token = jwt.encode(
        {"sub": "alice", "roles": ["operator"], "exp": time.time() + 60, **claims},
        SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


ALICE = _bearer({"azp": "orrery-console"})
AGENT_X = _bearer({"azp": "partner", "act": {"sub": "agent-x"}})
AGENT_Y = _bearer({"azp": "partner", "act": {"sub": "agent-y"}})


def _say(client: TestClient, headers: dict, text: str, session_id: str | None = None) -> dict:
    res = client.post("/chat", headers=headers, json={"message": text, "session_id": session_id})
    assert res.status_code == 200, res.text
    return res.json()


def _listed(client: TestClient, headers: dict) -> set[str]:
    res = client.get("/sessions", headers=headers)
    assert res.status_code == 200, res.text
    return {s["session_id"] for s in res.json()["sessions"]}


def test_an_agent_cannot_list_read_or_continue_its_users_thread(client):
    sid = _say(client, ALICE, "secret plan")["session_id"]

    assert sid not in _listed(client, AGENT_X)
    for path in (f"/session/{sid}", f"/session/{sid}/activity", f"/session/{sid}/triage"):
        assert client.get(path, headers=AGENT_X).status_code == 404, path
    assert client.delete(f"/session/{sid}", headers=AGENT_X).status_code == 404

    reply = _say(client, AGENT_X, "what did she say?", sid)
    assert reply["session_id"] != sid
    assert reply["response"] == "what did she say?"  # nothing of alice's thread
    # And alice's thread is intact.
    assert client.get(f"/session/{sid}", headers=ALICE).status_code == 200


def test_two_agents_for_one_user_stay_apart(client):
    x_sid = _say(client, AGENT_X, "x one")["session_id"]

    assert x_sid not in _listed(client, AGENT_Y)
    assert client.get(f"/session/{x_sid}", headers=AGENT_Y).status_code == 404
    assert _say(client, AGENT_Y, "y one", x_sid)["response"] == "y one"


def test_an_agent_continues_its_own_thread(client):
    first = _say(client, AGENT_X, "one")
    second = _say(client, AGENT_X, "two", first["session_id"])

    assert second["session_id"] == first["session_id"]
    assert second["response"] == "one|two"
    assert _listed(client, AGENT_X) == {first["session_id"]}
    assert client.get(f"/session/{first['session_id']}", headers=AGENT_X).status_code == 200


def test_the_user_still_sees_what_their_agents_did(client):
    x_sid = _say(client, AGENT_X, "x one")["session_id"]
    own = _say(client, ALICE, "mine")["session_id"]

    assert {x_sid, own} <= _listed(client, ALICE)
    assert client.get(f"/session/{x_sid}", headers=ALICE).status_code == 200
