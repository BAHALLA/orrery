"""Streaming turns (AEP-009): ProgressPlugin, gateway SSE mode, /chat/stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from orrery_core import confirm
from orrery_core.plugins.progress_plugin import ProgressPlugin, ProgressStep, progress_scope
from orrery_core.security.auth import JWTConfig
from orrery_core.serving.gateway import AgentGateway, InboundMessage, OutboundReply
from orrery_core.serving.server import ServerConfig, create_app
from orrery_core.serving.sse import (
    KEEPALIVE,
    TURN_FAILED_MESSAGE,
    drain_turns,
    encode_event,
    stream_turn,
)

# ── Helpers ──────────────────────────────────────────────────────────


class ScriptedLlm(BaseLlm):
    """Replays fixed model turns; a turn may be a list of streamed chunks."""

    model: str = "scripted"
    script: list[Any] = []

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        turn = self.script.pop(0)
        for response in turn if isinstance(turn, list) else [turn]:
            yield response


def _call(name: str, **args: Any) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part.from_function_call(name=name, args=args)]
        )
    )


def _text(text: str, *, partial: bool | None = None) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
        partial=partial,
    )


def _ctx(agent: str = "root", call_id: str = "c1") -> MagicMock:
    ctx = MagicMock()
    ctx.agent_name = agent
    ctx.function_call_id = call_id
    return ctx


def _tool(name: str, func: Any = None) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.func = func
    return tool


def _frames(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into ``(event, data)`` pairs, skipping comments."""
    frames = []
    for block in body.strip().split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln and not ln.startswith(":")]
        if not lines:
            continue
        event = next(ln[7:] for ln in lines if ln.startswith("event: "))
        data = json.loads(next(ln[6:] for ln in lines if ln.startswith("data: ")))
        frames.append((event, data))
    return frames


# ── ProgressPlugin ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_progress_plugin_is_inert_without_a_scope():
    plugin = ProgressPlugin()

    assert (
        await plugin.before_tool_callback(tool=_tool("t"), tool_args={}, tool_context=_ctx())
        is None
    )
    assert (
        await plugin.after_tool_callback(
            tool=_tool("t"), tool_args={}, tool_context=_ctx(), result={"status": "success"}
        )
        is None
    )
    assert plugin._started == {}


@pytest.mark.asyncio
async def test_progress_pairs_start_and_end_without_arguments():
    plugin = ProgressPlugin()
    steps: list[ProgressStep] = []

    with progress_scope(steps.append):
        await plugin.before_tool_callback(
            tool=_tool("get_logs"), tool_args={"token": "s3cret"}, tool_context=_ctx("k8s")
        )
        await plugin.after_tool_callback(
            tool=_tool("get_logs"),
            tool_args={"token": "s3cret"},
            tool_context=_ctx("k8s"),
            result={"status": "success", "logs": "s3cret in a log line"},
        )

    start, end = steps
    assert (start.phase, end.phase) == ("start", "end")
    assert start.call_id == end.call_id
    assert (end.agent, end.tool, end.outcome, end.status) == ("k8s", "get_logs", "ok", "success")
    assert end.elapsed_ms is not None and end.elapsed_ms >= 0
    # Neither arguments nor results reach the stream.
    assert "s3cret" not in json.dumps([s.as_dict() for s in steps])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        ({"status": "success"}, "ok"),
        ({"status": "error", "message": "broker down"}, "error"),
        ({"status": "access_denied"}, "refused"),
        ({"status": "confirmation_required"}, "refused"),
        ({"status": "AWAITING_CONFIRMATION"}, "refused"),
    ],
)
async def test_progress_outcome_tells_a_refusal_from_a_failure(result, outcome):
    plugin = ProgressPlugin()
    steps: list[ProgressStep] = []

    with progress_scope(steps.append):
        await plugin.after_tool_callback(
            tool=_tool("t"), tool_args={}, tool_context=_ctx(), result=result
        )

    assert steps[-1].outcome == outcome


@pytest.mark.asyncio
async def test_progress_flags_guarded_tools():
    @confirm("restarts a deployment")
    async def restart() -> dict:
        return {}

    plugin = ProgressPlugin()
    steps: list[ProgressStep] = []

    with progress_scope(steps.append):
        await plugin.before_tool_callback(
            tool=_tool("restart", restart), tool_args={}, tool_context=_ctx()
        )

    assert steps[0].guarded is True


@pytest.mark.asyncio
async def test_a_failing_sink_never_fails_the_turn():
    def sink(_: ProgressStep) -> None:
        raise RuntimeError("client went away")

    plugin = ProgressPlugin()
    with progress_scope(sink):
        assert (
            await plugin.before_tool_callback(tool=_tool("t"), tool_args={}, tool_context=_ctx())
            is None
        )


@pytest.mark.asyncio
async def test_started_map_is_bounded(monkeypatch):
    """A cancelled turn never reaches `end`; the map must not grow forever."""
    monkeypatch.setattr("orrery_core.plugins.progress_plugin._MAX_TRACKED_CALLS", 3)
    plugin = ProgressPlugin()

    with progress_scope(lambda _: None):
        for i in range(10):
            await plugin.before_tool_callback(
                tool=_tool("t"), tool_args={}, tool_context=_ctx(call_id=f"c{i}")
            )

    assert list(plugin._started) == ["c7", "c8", "c9"]


# ── A real ADK run: progress crosses AgentTool, streaming is not doubled ──


async def get_status() -> dict:
    """Read the service status."""
    return {"status": "success", "healthy": True}


@pytest.mark.asyncio
async def test_progress_reaches_inside_an_agent_tool_specialist():
    """The point of the plugin: a specialist's own tool calls are visible.

    ``AgentTool`` consumes its sub-Runner's events, so the root's event stream
    shows one opaque call. The plugin list is inherited by the sub-Runner, so
    the armed sink sees the inner call too.
    """
    specialist = LlmAgent(
        name="kafka_specialist",
        description="Kafka checks",
        model=ScriptedLlm(script=[_call("get_status"), _text("kafka is healthy")]),
        tools=[get_status],
    )
    root = LlmAgent(
        name="root",
        model=ScriptedLlm(script=[_call("kafka_specialist", request="check"), _text("All good.")]),
        tools=[AgentTool(agent=specialist)],
    )
    gateway = AgentGateway(
        app_name="t",
        root_agent=root,
        plugins=[ProgressPlugin()],
        session_service=InMemorySessionService(),
    )
    steps: list[ProgressStep] = []

    with progress_scope(steps.append):
        reply = await gateway.run(
            InboundMessage(text="check kafka", user_id="u", conversation_key="", channel="test")
        )

    seen = [(s.phase, s.agent, s.tool, s.delegation, s.outcome) for s in steps]
    assert seen == [
        ("start", "root", "kafka_specialist", True, None),
        ("start", "kafka_specialist", "get_status", False, None),
        ("end", "kafka_specialist", "get_status", False, "ok"),
        ("end", "root", "kafka_specialist", True, "ok"),
    ]
    assert reply.text == "All good."


@pytest.mark.asyncio
async def test_streamed_reply_is_not_doubled_by_its_partial_chunks():
    """Partial chunks repeat in the final aggregated event; count them once."""
    root = LlmAgent(
        name="root",
        model=ScriptedLlm(
            script=[
                [
                    _text("Clu", partial=True),
                    _text("ster ok", partial=True),
                    _text("Cluster ok", partial=False),
                ]
            ]
        ),
    )
    gateway = AgentGateway(
        app_name="t", root_agent=root, plugins=[], session_service=InMemorySessionService()
    )
    deltas: list[str] = []

    async def on_event(event: Any) -> None:
        if event.partial is True:
            deltas.append(event.content.parts[0].text)

    reply = await gateway.run(
        InboundMessage(text="status?", user_id="u", conversation_key="", channel="test"),
        on_event=on_event,
        streaming=True,
    )

    assert deltas == ["Clu", "ster ok"]
    assert reply.text == "Cluster ok"


# ── SSE framing ──────────────────────────────────────────────────────


class _FakeGateway:
    """Stands in for AgentGateway.run inside stream_turn."""

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.kwargs: dict[str, Any] = {}

    async def run(self, msg, *, on_event=None, session_id=None, streaming=False) -> OutboundReply:
        assert on_event is not None and session_id is not None
        self.kwargs = {"session_id": session_id, "streaming": streaming}
        # Progress from inside the turn's own task: the scope must reach it.
        plugin = ProgressPlugin()
        await plugin.before_tool_callback(
            tool=_tool("get_status"), tool_args={}, tool_context=_ctx()
        )
        partial = MagicMock(partial=True, author="root")
        partial.content.parts = [MagicMock(text="Hel", thought=None)]
        await on_event(partial)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider said: secret-internal-hostname:443 refused")
        return OutboundReply(text="Hello", session_id=session_id)


def _msg() -> InboundMessage:
    return InboundMessage(text="hi", user_id="u", conversation_key="", channel="http")


async def _collect(gen) -> list[bytes]:
    return [frame async for frame in gen]


@pytest.mark.asyncio
async def test_stream_turn_frames_in_order():
    gateway = _FakeGateway()

    frames = await _collect(stream_turn(gateway, _msg(), session_id="s1"))

    events = _frames(b"".join(frames).decode())
    assert [e for e, _ in events] == ["session", "progress", "delta", "message", "done"]
    assert events[0][1] == {"session_id": "s1"}
    assert events[1][1]["tool"] == "get_status"
    assert events[2][1] == {"text": "Hel", "author": "root"}
    assert events[3][1] == {"session_id": "s1", "text": "Hello"}
    assert gateway.kwargs == {"session_id": "s1", "streaming": True}


@pytest.mark.asyncio
async def test_stream_turn_reports_failure_without_leaking_the_exception(caplog):
    frames = await _collect(stream_turn(_FakeGateway(fail=True), _msg(), session_id="s1"))

    body = b"".join(frames).decode()
    events = _frames(body)
    assert [e for e, _ in events][-2:] == ["error", "done"]
    assert events[-2][1] == {"message": TURN_FAILED_MESSAGE, "retryable": True}
    assert "secret-internal-hostname" not in body
    assert "secret-internal-hostname" in caplog.text  # …but the operator can see it


@pytest.mark.asyncio
async def test_stream_turn_sends_keepalives_during_a_long_call():
    frames = await _collect(
        stream_turn(_FakeGateway(delay=0.2), _msg(), session_id="s1", keepalive_seconds=0.05)
    )

    assert KEEPALIVE in frames


@pytest.mark.asyncio
async def test_turn_keeps_running_after_the_client_disconnects():
    """Abandoning the stream must not cancel the agent mid-tool."""
    finished = asyncio.Event()

    class SlowGateway(_FakeGateway):
        async def run(
            self, msg, *, on_event=None, session_id=None, streaming=False
        ) -> OutboundReply:
            await asyncio.sleep(0.05)
            finished.set()
            return OutboundReply(text="done", session_id=session_id or "")

    gen = stream_turn(SlowGateway(), _msg(), session_id="s1")
    assert b"session" in await gen.__anext__()
    # The next frame is where the generator starts the turn and then waits on
    # it. Starlette cancels exactly this await when the client disconnects.
    waiting = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await gen.aclose()

    await asyncio.wait_for(finished.wait(), timeout=1)


async def _abandoned_turn(seconds: float, finished: asyncio.Event) -> None:
    """Start a streamed turn of *seconds*, then drop its client."""

    class SlowGateway(_FakeGateway):
        async def run(
            self, msg, *, on_event=None, session_id=None, streaming=False
        ) -> OutboundReply:
            await asyncio.sleep(seconds)
            finished.set()
            return OutboundReply(text="done", session_id=session_id or "")

    gen = stream_turn(SlowGateway(), _msg(), session_id="s1")
    await gen.__anext__()
    waiting = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await gen.aclose()


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_turn_whose_client_left():
    # The server stops waiting for a request once its client is gone; the turn
    # keeps running, so shutdown has to wait for it or SIGTERM cuts it mid-tool.
    finished = asyncio.Event()
    await _abandoned_turn(0.1, finished)

    assert await drain_turns(timeout=2) == 0
    assert finished.is_set()


@pytest.mark.asyncio
async def test_shutdown_waits_no_longer_than_its_grace():
    finished = asyncio.Event()
    await _abandoned_turn(5, finished)

    assert await drain_turns(timeout=0.05) == 1
    assert not finished.is_set()
    from orrery_core.serving import sse

    for task in list(sse._running_turns):
        task.cancel()


def test_encode_event_is_one_frame():
    frame = encode_event("delta", {"text": "two\nlines"})

    assert frame == b'event: delta\ndata: {"text":"two\\nlines"}\n\n'


# ── /chat/stream over HTTP ───────────────────────────────────────────


@pytest.fixture
def streaming_app():
    seen: dict[str, Any] = {}

    async def fake_run_async(
        *, user_id, session_id, new_message, state_delta=None, run_config=None
    ):
        seen["run_config"] = run_config
        for text, partial in (("Hel", True), ("lo", True), ("Hello", False)):
            event = MagicMock(partial=partial, author="root")
            event.content.parts = [MagicMock(text=text, thought=None)]
            yield event

    runner = MagicMock()
    runner.run_async = fake_run_async
    session = MagicMock(id="sess-1")
    sessions = MagicMock()
    sessions.create_session = AsyncMock(return_value=session)
    sessions.get_session = AsyncMock(return_value=None)

    with (
        patch("orrery_core.serving.gateway.Runner", return_value=runner),
        patch("orrery_core.serving.gateway.App", return_value=MagicMock()),
        patch("orrery_core.serving.server.create_session_service", return_value=sessions),
    ):
        config = ServerConfig(auth_enabled=False, jwt=JWTConfig(algorithm="HS256", secret="x"))
        app = create_app(root_agent=MagicMock(name="root"), app_name="t", plugins=[], config=config)
        yield app, seen


def test_chat_stream_endpoint_streams_the_turn(streaming_app):
    app, seen = streaming_app
    client = TestClient(app)

    response = client.post("/chat/stream", json={"message": "hi"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    events = _frames(response.text)
    assert [e for e, _ in events] == ["session", "delta", "delta", "message", "done"]
    assert events[-2][1] == {"session_id": "sess-1", "text": "Hello"}
    assert seen["run_config"] is not None  # SSE mode was requested from ADK


def test_chat_stream_requires_auth_when_enabled(streaming_app):
    with (
        patch("orrery_core.serving.server.create_session_service"),
        patch("orrery_core.serving.gateway.Runner"),
        patch("orrery_core.serving.gateway.App"),
    ):
        config = ServerConfig(auth_enabled=True, jwt=JWTConfig(algorithm="HS256", secret="x" * 32))
        app = create_app(root_agent=MagicMock(name="root"), app_name="t", plugins=[], config=config)

    assert TestClient(app).post("/chat/stream", json={"message": "hi"}).status_code == 401


def test_plain_chat_reply_is_unchanged_by_partial_events(streaming_app):
    """The non-streaming endpoint must not double-count a partial chunk either."""
    app, _ = streaming_app

    response = TestClient(app).post("/chat", json={"message": "hi"})

    assert response.json()["response"] == "Hello"
