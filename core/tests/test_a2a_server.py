"""Tests for orrery_core.serving.a2a_server (ADR-007: agents over A2A)."""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest

pytest.importorskip("a2a")
pytest.importorskip("fastapi")

from a2a.types.a2a_pb2 import TaskState  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402

from orrery_core.security.auth import AUTH_STATE_KEY, JWTConfig  # noqa: E402
from orrery_core.security.confirmation_store import (  # noqa: E402
    ConfirmationStore,
    PendingConfirmation,
)
from orrery_core.serving.a2a_config import A2AConfig  # noqa: E402
from orrery_core.serving.a2a_server import session_id_for  # noqa: E402
from orrery_core.serving.gateway import AgentGateway, OutboundReply  # noqa: E402
from orrery_core.serving.server import ServerConfig, create_app  # noqa: E402

_SECRET = "x" * 64
_A2A_AUD = "orrery-a2a"
_TASK_NOT_FOUND = -32001


def _headers(sub: str = "bot", *, aud: str = _A2A_AUD, roles=("operator",), **extra) -> dict:
    claims = {"sub": sub, "roles": list(roles), "exp": int(time.time()) + 600, "aud": aud}
    token = pyjwt.encode({**claims, **extra}, _SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "A2A-Version": "1.0"}


def _send(text: str | None = "hello", *, context_id: str = "", task_id: str = "", parts=None):
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": parts if parts is not None else [{"text": text}],
    }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }


@pytest.fixture
def pending_store():
    from orrery_core.security.guardrails import _pending_confirmations

    store = ConfirmationStore()
    _pending_confirmations.configure(store)
    yield store
    _pending_confirmations.configure(None)


@pytest.fixture
def turns():
    """Replaces the gateway turn; records each call's keyword arguments."""
    calls: list[dict] = []

    async def fake_turn(self, **kwargs):
        calls.append(kwargs)
        return OutboundReply(text=f"echo:{kwargs['text']}", session_id=kwargs["session_id"])

    with (
        patch("orrery_core.serving.gateway.Runner", return_value=MagicMock()),
        patch("orrery_core.serving.gateway.App", return_value=MagicMock()),
        patch(
            "orrery_core.serving.server.create_session_service",
            return_value=InMemorySessionService(),
        ),
        patch.object(AgentGateway, "run_in_session", fake_turn),
    ):
        yield calls


def _app(**a2a):
    config = ServerConfig(
        auth_enabled=True,
        jwt=JWTConfig(algorithm="HS256", secret=_SECRET, audience="orrery"),
        a2a=A2AConfig(enabled=True, audience=_A2A_AUD, **a2a),
    )
    root = MagicMock(name="root")
    root.description = "SRE coordinator"
    root.tools = []
    return create_app(root_agent=root, app_name="test", plugins=[], config=config)


def _result(response) -> dict:
    body = response.json()
    assert "error" not in body, body
    return body["result"]["task"]


# ── Startup ──────────────────────────────────────────────────────────


def test_a2a_needs_an_audience():
    with pytest.raises(ValueError, match="ORRERY_A2A_JWT_AUDIENCE"):
        A2AConfig(enabled=True).validate(auth_enabled=True)


def test_a2a_needs_auth():
    with pytest.raises(ValueError, match="AUTH_ENABLED"):
        A2AConfig(enabled=True, audience="a").validate(auth_enabled=False)


def test_a2a_advertises_https_outside_loopback():
    cfg = A2AConfig(enabled=True, audience="a", public_url="http://orrery.example.com/a2a")
    with pytest.raises(ValueError, match="https"):
        cfg.validate(auth_enabled=True)


def test_a2a_refuses_a_replicated_deployment(monkeypatch, turns):
    monkeypatch.setenv("ORRERY_MULTI_REPLICA", "true")
    monkeypatch.setenv("ORRERY_CONFIRMATION_BACKEND", "postgres")
    config = ServerConfig(
        auth_enabled=True,
        jwt=JWTConfig(algorithm="HS256", secret=_SECRET, audience="orrery"),
        database_url="postgresql://x/y",
        a2a=A2AConfig(enabled=True, audience=_A2A_AUD),
    )
    with pytest.raises(RuntimeError, match="ORRERY_A2A_ENABLED"):
        create_app(root_agent=MagicMock(), app_name="t", plugins=[], config=config)


def test_a2a_is_off_by_default(turns):
    config = ServerConfig(
        auth_enabled=True, jwt=JWTConfig(algorithm="HS256", secret=_SECRET, audience="orrery")
    )
    client = TestClient(create_app(root_agent=MagicMock(), app_name="t", config=config))
    assert client.post("/a2a", json=_send(), headers=_headers()).status_code in (404, 405)


# ── Who may call ─────────────────────────────────────────────────────


def test_anonymous_calls_are_refused(turns):
    r = TestClient(_app()).post("/a2a", json=_send())
    assert r.status_code == 401
    assert turns == []


def test_a_console_token_is_not_an_agent_token(turns):
    r = TestClient(_app()).post("/a2a", json=_send(), headers=_headers(aud="orrery"))
    assert r.status_code == 401
    assert turns == []


def test_a_call_from_orrery_itself_is_refused(turns):
    headers = {**_headers(), "X-Orrery-Hop": "1"}
    r = TestClient(_app()).post("/a2a", json=_send(), headers=headers)
    assert r.status_code == 403
    assert turns == []


def test_calls_are_rate_limited_per_caller(turns):
    client = TestClient(_app(rate_limit="1/minute"))
    assert client.post("/a2a", json=_send(), headers=_headers("bot")).status_code == 200
    assert client.post("/a2a", json=_send(), headers=_headers("bot")).status_code == 429
    assert client.post("/a2a", json=_send(), headers=_headers("other")).status_code == 200


# ── A turn ───────────────────────────────────────────────────────────


def test_a_turn_runs_through_the_gateway_as_a_non_deciding_service(turns):
    r = TestClient(_app()).post("/a2a", json=_send("check kafka"), headers=_headers("bot"))
    task = _result(r)

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"][0]["text"] == "echo:check kafka"
    (call,) = turns
    assert call["user_id"] == "bot"
    assert call["can_decide"] is False
    auth = call["state_delta"][AUTH_STATE_KEY]
    assert auth["principal_kind"] == "service"
    # No ceiling configured for the client: a service is capped at viewer.
    assert auth["role"] == "viewer"
    # A service acts for no one: impersonating systems refuse it.
    assert call["identity"] is None


def test_a_delegated_agent_acts_for_its_user(turns):
    headers = _headers("alice", act={"sub": "partner-agent"}, email="a@x.io", email_verified=True)
    TestClient(_app()).post("/a2a", json=_send(), headers=headers)

    (call,) = turns
    auth = call["state_delta"][AUTH_STATE_KEY]
    assert auth["principal_kind"] == "delegated"
    assert auth["acting_agent"] == "partner-agent"
    assert call["can_decide"] is False
    assert call["identity"] is not None


def test_non_text_parts_are_refused(turns):
    parts = [{"data": {"name": "kubectl", "response": {"ok": True}}}]
    r = TestClient(_app()).post("/a2a", json=_send(parts=parts), headers=_headers())
    task = _result(r)
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert turns == []


def test_a_context_maps_to_a_session_of_that_caller_only(turns):
    client = TestClient(_app())
    first = _result(client.post("/a2a", json=_send(), headers=_headers("bot")))
    ctx = first["contextId"]
    client.post("/a2a", json=_send(context_id=ctx), headers=_headers("bot"))
    client.post("/a2a", json=_send(context_id=ctx), headers=_headers("intruder"))

    bot_1, bot_2, intruder = (c["session_id"] for c in turns)
    assert bot_1 == bot_2 == session_id_for("bot", ctx)
    assert intruder == session_id_for("intruder", ctx) != bot_1


def test_a_context_never_names_a_console_session():
    # A delegated agent for alice must not continue alice's console thread by
    # sending its id as the contextId.
    assert session_id_for("alice", "sess-1") != "sess-1"
    assert session_id_for("alice", "sess-1").startswith("a2a-")


def test_tasks_are_visible_to_their_owner_only(turns):
    client = TestClient(_app())
    task = _result(client.post("/a2a", json=_send(), headers=_headers("bot")))
    get = {"jsonrpc": "2.0", "id": "2", "method": "GetTask", "params": {"id": task["id"]}}

    mine = client.post("/a2a", json=get, headers=_headers("bot")).json()
    theirs = client.post("/a2a", json=get, headers=_headers("intruder")).json()

    assert mine["result"]["id"] == task["id"]
    assert theirs["error"]["code"] == _TASK_NOT_FOUND


def test_listing_tasks_shows_only_the_callers_own(turns):
    client = TestClient(_app())
    client.post("/a2a", json=_send(), headers=_headers("bot"))
    listing = {"jsonrpc": "2.0", "id": "2", "method": "ListTasks", "params": {}}

    assert (
        len(client.post("/a2a", json=listing, headers=_headers("bot")).json()["result"]["tasks"])
        == 1
    )
    assert (
        client.post("/a2a", json=listing, headers=_headers("intruder")).json()["result"]["tasks"]
        == []
    )


def test_another_caller_cannot_continue_a_task(turns):
    client = TestClient(_app())
    task = _result(client.post("/a2a", json=_send(), headers=_headers("bot")))
    follow_up = _send(task_id=task["id"], context_id=task["contextId"])

    r = client.post("/a2a", json=follow_up, headers=_headers("intruder")).json()
    assert r["error"]["code"] == _TASK_NOT_FOUND
    assert len(turns) == 1


@pytest.mark.asyncio
async def test_a_running_turn_cannot_be_cancelled():
    from a2a.utils.errors import TaskNotCancelableError

    from orrery_core.serving.a2a_server import OrreryAgentExecutor

    executor = OrreryAgentExecutor(MagicMock(), caller_identity=lambda _a: None, approval_base="")
    # Stopping the wait would not stop a scale already sent to the cluster.
    with pytest.raises(TaskNotCancelableError):
        await executor.cancel(MagicMock(), MagicMock())


@pytest.mark.asyncio
async def test_concurrent_turns_per_caller_are_capped():
    import asyncio

    from orrery_core.security.auth import AuthContext
    from orrery_core.serving.a2a_server import OrreryAgentExecutor

    release = asyncio.Event()
    gateway = MagicMock()
    gateway.app_name = "t"
    gateway.sessions = InMemorySessionService()

    async def slow_turn(**kwargs):
        await release.wait()
        return OutboundReply(text="done", session_id=kwargs["session_id"])

    gateway.run_in_session = slow_turn
    executor = OrreryAgentExecutor(
        gateway, caller_identity=lambda _a: None, approval_base="", max_tasks_in_flight=1
    )
    auth = AuthContext(subject="bot", role="viewer", claims={}, principal_kind="service")

    def context(task_id):
        ctx = MagicMock()
        ctx.call_context.state = {"auth": auth}
        # An existing task: skips the leading Task event, which needs a real Message.
        ctx.task_id, ctx.context_id, ctx.current_task = task_id, "c", MagicMock()
        part = MagicMock()
        part.WhichOneof.return_value = "text"
        part.text = "hi"
        ctx.message.parts = [part]
        return ctx

    queues = [MagicMock(), MagicMock()]
    for q in queues:
        q.enqueue_event = AsyncMock()
    first = asyncio.create_task(executor.execute(context("t1"), queues[0]))
    await asyncio.sleep(0.01)
    await executor.execute(context("t2"), queues[1])
    release.set()
    await first

    def states(queue):
        return [
            getattr(c.args[0], "status", None) and c.args[0].status.state
            for c in queue.enqueue_event.call_args_list
        ]

    assert TaskState.TASK_STATE_REJECTED in states(queues[1])
    assert TaskState.TASK_STATE_COMPLETED in states(queues[0])


# ── Approvals ────────────────────────────────────────────────────────


def test_a_pending_action_asks_for_input_with_its_id(turns, pending_store):
    client = TestClient(_app())

    async def raise_pending(self, **kwargs):
        turns.append(kwargs)
        pending_store.add(
            PendingConfirmation(
                action_id="act-1",
                tool_name="scale_deployment",
                requester="alice",
                scope_key="alice",
                level="confirm",
                args={"replicas": 3, "token": "ghp_" + "a" * 36},
                session_id=kwargs["session_id"],
            )
        )
        return OutboundReply(text="needs approval", session_id=kwargs["session_id"])

    headers = _headers("alice", act={"sub": "partner-agent"})
    with patch.object(AgentGateway, "run_in_session", raise_pending):
        task = _result(client.post("/a2a", json=_send("scale it"), headers=headers))

    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    parts = task["status"]["message"]["parts"]
    assert parts[0]["text"] == "needs approval"
    payload = parts[1]["data"]
    assert payload["kind"] == "orrery.pending_action"
    assert payload["action_id"] == "act-1"
    assert payload["approve_url"].endswith("/confirmations/act-1/decision")
    assert payload["args"]["replicas"] == 3
    assert "ghp_" not in str(payload["args"])  # redacted like any tool result


def test_after_approval_the_client_resumes_the_task(turns, pending_store):
    client = TestClient(_app())
    headers = _headers("alice", act={"sub": "partner-agent"})
    pending = {"done": False}

    async def turn(self, **kwargs):
        turns.append(kwargs)
        if not pending["done"]:
            pending["done"] = True
            pending_store.add(
                PendingConfirmation(
                    action_id="act-2",
                    tool_name="scale_deployment",
                    requester="alice",
                    scope_key="alice",
                    level="confirm",
                    args={},
                    session_id=kwargs["session_id"],
                )
            )
            return OutboundReply(text="needs approval", session_id=kwargs["session_id"])
        pending_store.pop("act-2")  # the gate consumed the out-of-band approval
        return OutboundReply(text="scaled", session_id=kwargs["session_id"])

    with patch.object(AgentGateway, "run_in_session", turn):
        task = _result(client.post("/a2a", json=_send("scale it"), headers=headers))
        resumed = _result(
            client.post(
                "/a2a",
                json=_send("approved, go on", task_id=task["id"], context_id=task["contextId"]),
                headers=headers,
            )
        )

    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert resumed["id"] == task["id"]
    assert resumed["status"]["state"] == "TASK_STATE_COMPLETED"
    assert turns[0]["session_id"] == turns[1]["session_id"]
    # The resuming message is still not a decision.
    assert turns[1]["can_decide"] is False


def test_another_conversations_pending_does_not_hold_this_task(turns, pending_store):
    pending_store.add(
        PendingConfirmation(
            action_id="elsewhere",
            tool_name="scale_deployment",
            requester="alice",
            scope_key="alice",
            level="confirm",
            args={},
            session_id="console-thread",
        )
    )
    headers = _headers("alice", act={"sub": "partner-agent"})
    task = _result(TestClient(_app()).post("/a2a", json=_send(), headers=headers))
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"


# ── Agent card ───────────────────────────────────────────────────────


def test_the_public_card_names_the_bearer_scheme_not_the_specialists(turns):
    r = TestClient(_app()).get("/a2a/.well-known/agent-card.json")
    card = r.json()
    assert r.status_code == 200
    assert card["name"] == "test"
    assert card["supportedInterfaces"][0]["url"].endswith("/a2a")
    assert "bearer" in card["securitySchemes"]
    assert card["capabilities"]["pushNotifications"] is False
    # One generic skill (the spec requires one); the specialists are not listed.
    assert [s["id"] for s in card["skills"]] == ["investigate"]


def test_the_extended_card_lists_live_specialists(turns):
    from google.adk.agents import Agent
    from google.adk.tools.agent_tool import AgentTool

    kafka = Agent(name="kafka_health_agent", description="Kafka lag and brokers")
    root = Agent(name="root", description="coordinator", tools=[AgentTool(agent=kafka)])
    config = ServerConfig(
        auth_enabled=True,
        jwt=JWTConfig(algorithm="HS256", secret=_SECRET, audience="orrery"),
        a2a=A2AConfig(enabled=True, audience=_A2A_AUD),
    )
    client = TestClient(create_app(root_agent=root, app_name="t", plugins=[], config=config))
    req = {"jsonrpc": "2.0", "id": "4", "method": "GetExtendedAgentCard", "params": {}}

    card = client.post("/a2a", json=req, headers=_headers()).json()["result"]
    assert [s["id"] for s in card["skills"]] == ["kafka_health_agent"]
    assert card["skills"][0]["description"] == "Kafka lag and brokers"


def test_a_streamed_turn_ends_completed(turns):
    body = {**_send("stream it"), "method": "SendStreamingMessage"}
    with TestClient(_app()).stream("POST", "/a2a", json=body, headers=_headers()) as r:
        assert r.status_code == 200
        frames = [line for line in r.iter_lines() if line.startswith("data:")]

    assert '"TASK_STATE_COMPLETED"' in frames[-1]
    assert any("echo:stream it" in f for f in frames)


@pytest.mark.asyncio
async def test_the_a2a_sdk_client_completes_a_turn(turns):
    """The documented client snippet, end to end against the app."""
    import httpx
    from a2a.client import ClientConfig, create_client
    from a2a.helpers import new_text_message
    from a2a.types.a2a_pb2 import SendMessageRequest

    transport = httpx.ASGITransport(app=_app())
    headers = {"Authorization": _headers()["Authorization"]}
    async with httpx.AsyncClient(transport=transport, headers=headers) as http:
        client = await create_client(
            "http://127.0.0.1:8000/a2a", client_config=ClientConfig(httpx_client=http)
        )
        request = SendMessageRequest(message=new_text_message("Is orders lagging?"))
        events = [event async for event in client.send_message(request)]

    final = events[-1]
    assert final.WhichOneof("payload") in {"task", "status_update"}
    assert any("echo:Is orders lagging?" in str(e) for e in events)
