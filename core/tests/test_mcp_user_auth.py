"""Per-user MCP credentials (ADR-004) against a real streamable-HTTP server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("mcp")

import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from orrery_core.reliability.resilience import (  # noqa: E402
    CircuitBreaker,
    ToolOutcome,
    classify_tool_outcome,
)
from orrery_core.security.identity import CallerIdentity, caller_scope  # noqa: E402
from orrery_core.security.token_exchange import (  # noqa: E402
    CredentialUnavailable,
    ExchangeSettings,
    TokenBroker,
)
from orrery_core.tools.mcp import GuardedMcpToolset, McpServerConfig, _with_deadline  # noqa: E402
from orrery_core.tools.mcp_network import (  # noqa: E402
    GuardedTransport,
    _ByteCap,
    _McpCappedStream,
    httpx,
)


class Recorder:
    """ASGI middleware: which Authorization each MCP session carried."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.by_session: dict[str, set[str]] = defaultdict(set)
        self.unauthenticated = 0
        self.rejected: set[str] = set()
        #: Rejected on POST only, the shape that kills an SSE session.
        self.rejected_posts: set[str] = set()
        #: Rejected on the optional GET stream only, as some gateways do.
        self.rejected_gets: set[str] = set()
        #: Answer every POST with 500, as during an outage.
        self.outage = False
        #: The status an outage answers with.
        self.outage_status = 500
        #: Answer this many POSTs with a same-origin 307 to the same URL.
        self.redirects = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization")
        if auth is None:
            self.unauthenticated += 1
        method = scope["method"]
        if self.redirects and method == "POST":
            self.redirects -= 1
            path = scope["path"].encode() + b"?" + scope["query_string"]
            await send(
                {"type": "http.response.start", "status": 307, "headers": [(b"location", path)]}
            )
            return await send({"type": "http.response.body", "body": b""})
        if self.outage and method == "POST":
            await send({"type": "http.response.start", "status": self.outage_status, "headers": []})
            return await send({"type": "http.response.body", "body": b""})
        if (
            auth in self.rejected
            or (method == "POST" and auth in self.rejected_posts)
            or (method == "GET" and auth in self.rejected_gets)
        ):
            await send({"type": "http.response.start", "status": 401, "headers": []})
            return await send({"type": "http.response.body", "body": b""})

        async def recording_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                sent = {k.decode().lower(): v.decode() for k, v in message["headers"]}
                session = headers.get("mcp-session-id") or sent.get("mcp-session-id")
                if session and auth:
                    self.by_session[session].add(auth)
            await send(message)

        await self.app(scope, receive, recording_send)


@pytest.fixture(scope="module")
def http_server() -> Iterator[tuple[str, Recorder]]:
    server = MCPServer("inventory")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def list_items() -> list[str]:
        """List inventory items."""
        return ["widget"]

    recorder = Recorder(server.streamable_http_app())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(recorder, log_level="warning", lifespan="on")
    runner = uvicorn.Server(config)
    thread = threading.Thread(target=runner.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not runner.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runner.started, "MCP test server did not start"
    yield f"http://127.0.0.1:{port}/mcp", recorder
    runner.should_exit = True
    thread.join(timeout=5)


class FakeIdP:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls += 1
        form = kwargs["data"]
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "access_token": f"x-{form['subject_token']}-{form['audience']}",
            "token_type": "Bearer",
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "expires_in": 300,
        }
        return response


def _config(**over: Any) -> McpServerConfig:
    base: dict[str, Any] = {
        "name": "inv",
        "description": "Inventory service",
        "transport": "streamable_http",
        "url": "https://mcp.example.com/mcp",
        "tools": ["list_items"],
        "read_only": ["list_items"],
        "auth": {"type": "token_exchange", "audience": "inventory-mcp"},
    }
    return McpServerConfig(**{**base, **over})


def _broker(idp: FakeIdP | None = None) -> TokenBroker:
    settings = ExchangeSettings(url="https://idp.example.com/t", client_id="c", client_secret="s")
    return TokenBroker(settings, session=cast(Any, idp or FakeIdP()))


def _as(token: str | None, subject: str):
    return caller_scope(CallerIdentity(subject=subject, username=subject, subject_token=token))


# ── Config ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("header", ["Authorization", "authorization"])
def test_auth_and_a_shared_authorization_header_are_exclusive(header):
    with pytest.raises(ValidationError, match="remove it from `headers_from_env`"):
        _config(headers_from_env={header: "SHARED_TOKEN"})


def test_auth_needs_an_http_transport():
    with pytest.raises(ValidationError, match="takes no `url` / `headers_from_env` / `auth`"):
        McpServerConfig(
            name="inv",
            description="x",
            transport="stdio",
            command="inventory-mcp",
            tools=["list_items"],
            auth={"type": "token_exchange", "audience": "inventory-mcp"},
        )


def test_unknown_auth_types_and_empty_audiences_are_refused():
    with pytest.raises(ValidationError):
        _config(auth={"type": "oauth_consent", "audience": "x"})
    with pytest.raises(ValidationError):
        _config(auth={"type": "token_exchange", "audience": ""})


def test_missing_exchange_settings_fail_when_the_toolset_is_built(monkeypatch):
    from orrery_core.security import token_exchange

    token_exchange.reset_default_broker()
    monkeypatch.delenv("ORRERY_TOKEN_EXCHANGE_URL", raising=False)
    with pytest.raises(ValueError, match="ORRERY_TOKEN_EXCHANGE_URL"):
        GuardedMcpToolset(_config())
    token_exchange.reset_default_broker()


# ── Against a real server ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_user_reaches_the_server_with_their_own_token_and_session(http_server):
    url, recorder = http_server
    idp = FakeIdP()
    toolset = GuardedMcpToolset(_config(url=url), broker=_broker(idp))
    ctx = MagicMock()
    try:
        results = {}
        for user in ("alice", "bob", "alice"):
            with _as(f"console-{user}", user):
                (tool,) = await toolset.get_tools(ctx)
                results[user] = await tool.run_async(args={}, tool_context=ctx)
    finally:
        await toolset.close()

    assert all(classify_tool_outcome(r) is ToolOutcome.SUCCESS for r in results.values())
    assert recorder.unauthenticated == 0
    seen = {auth for auths in recorder.by_session.values() for auth in auths}
    assert seen == {
        "Bearer x-console-alice-inventory-mcp",
        "Bearer x-console-bob-inventory-mcp",
    }
    # No MCP session ever carried two people's tokens.
    assert all(len(auths) == 1 for auths in recorder.by_session.values())
    assert idp.calls == 2  # alice's second turn used the cached token


@pytest.mark.asyncio
async def test_a_caller_without_a_token_is_offered_nothing(http_server):
    url, recorder = http_server
    before = recorder.unauthenticated
    toolset = GuardedMcpToolset(_config(url=url), broker=_broker())
    try:
        with _as(None, "slack:U123"):
            assert await toolset.get_tools(MagicMock()) == []
        assert await toolset.get_tools(MagicMock()) == []  # outside any turn
        assert await toolset.get_tools(None) == []  # no context: no headers
    finally:
        await toolset.close()
    assert recorder.unauthenticated == before  # nothing went out without a token


@pytest.mark.asyncio
async def test_a_call_whose_credential_is_refused_is_answered_not_raised(http_server):
    url, _ = http_server

    class Refusing(TokenBroker):
        reason: Any = "refused"

        async def token_for(self, audience: str, scope: str | None = None) -> str:
            raise CredentialUnavailable("the IdP refused", reason=self.reason)

    toolset = GuardedMcpToolset(_config(url=url), broker=_broker())
    ctx = MagicMock()
    try:
        with _as("console-alice", "alice"):
            (tool,) = await toolset.get_tools(ctx)
        # The token went stale between listing and calling (a revoked grant).
        broker = Refusing(ExchangeSettings(url="https://i/t", client_id="c", client_secret="s"))
        toolset._broker = broker
        with _as("console-alice", "alice"):
            refused = await tool.run_async(args={}, tool_context=ctx)
            broker.reason = "unavailable"
            unreachable = await tool.run_async(args={}, tool_context=ctx)
    finally:
        await toolset.close()

    assert refused["error_type"] == "credential_unavailable"
    assert refused["message"].startswith("inv_list_items:")
    # A refusal is not the server failing; an unreachable IdP is a failure.
    assert classify_tool_outcome(refused) is ToolOutcome.IGNORE
    assert classify_tool_outcome(unreachable) is ToolOutcome.FAILURE


@pytest.mark.asyncio
async def test_static_servers_are_unchanged(http_server, monkeypatch):
    url, recorder = http_server
    monkeypatch.setenv("INV_TOKEN", "Bearer shared")
    toolset = GuardedMcpToolset(
        _config(url=url, auth=None, headers_from_env={"Authorization": "INV_TOKEN"})
    )
    try:
        (tool,) = await toolset.get_tools(MagicMock())
        result = await tool.run_async(args={}, tool_context=MagicMock())
    finally:
        await toolset.close()
    assert classify_tool_outcome(result) is ToolOutcome.SUCCESS
    assert any("Bearer shared" in auths for auths in recorder.by_session.values())


def test_token_exchange_without_a_scope_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="orrery.tools.mcp"):
        _config(auth={"type": "token_exchange", "audience": "inventory-mcp"})
    assert "requests no scope" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="orrery.tools.mcp"):
        _config(auth={"type": "token_exchange", "audience": "inventory-mcp", "scope": "read"})
    assert "requests no scope" not in caplog.text


class RotatingIdP(FakeIdP):
    """Issues a different token each time, as a real IdP does."""

    def post(self, url: str, **kwargs: Any) -> Any:
        response = super().post(url, **kwargs)
        response.json.return_value["access_token"] = f"rotated-{self.calls}"
        return response


@pytest.mark.asyncio
async def test_a_token_the_server_rejects_is_replaced_on_the_next_call(http_server):
    url, recorder = http_server
    now = [0.0]
    idp = RotatingIdP()
    settings = ExchangeSettings(url="https://idp.example.com/t", client_id="c", client_secret="s")
    broker = TokenBroker(settings, session=cast(Any, idp), clock=lambda: now[0])
    toolset = GuardedMcpToolset(_config(url=url, call_timeout_seconds=5), broker=broker)
    ctx = MagicMock()
    try:
        with _as("console-carol", "carol"):
            (tool,) = await toolset.get_tools(ctx)
            assert classify_tool_outcome(await tool.run_async(args={}, tool_context=ctx)) is (
                ToolOutcome.SUCCESS
            )
            assert idp.calls == 1

            recorder.rejected.add("Bearer rotated-1")
            now[0] += 60
            rejected = await tool.run_async(args={}, tool_context=ctx)

            # Evicted by the 401: this call exchanges again and gets through.
            result = await tool.run_async(args={}, tool_context=ctx)
            # The rejected token's session went with it.
            pooled = len(toolset._mcp_session_manager._sessions)
    finally:
        recorder.rejected.clear()
        await toolset.close()

    assert idp.calls == 2
    assert classify_tool_outcome(result) is ToolOutcome.SUCCESS
    assert pooled == 1
    assert rejected["error_type"] == "credential_rejected"
    assert classify_tool_outcome(rejected) is ToolOutcome.FAILURE


@pytest.fixture(scope="module")
def sse_server() -> Iterator[tuple[str, Recorder]]:
    server = MCPServer("inventory")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def list_items() -> list[str]:
        """List inventory items."""
        return ["widget"]

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def slow_item() -> str:
        """Answer after a moment, for a call in flight."""
        await asyncio.sleep(1)
        return "done"

    recorder = Recorder(server.sse_app())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    runner = uvicorn.Server(uvicorn.Config(recorder, log_level="warning"))
    thread = threading.Thread(target=runner.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not runner.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runner.started, "MCP test server did not start"
    yield f"http://127.0.0.1:{port}/sse", recorder
    runner.should_exit = True
    runner.force_exit = True
    thread.join(timeout=5)


@pytest.mark.asyncio
async def test_a_young_token_rejected_over_sse_does_not_wedge_the_user(sse_server):
    """The pool must not keep handing back the session that the 401 killed."""
    url, recorder = sse_server
    idp = RotatingIdP()
    toolset = GuardedMcpToolset(
        _config(transport="sse", url=url, call_timeout_seconds=2), broker=_broker(idp)
    )
    ctx = MagicMock()
    try:
        with _as("console-dave", "dave"):
            (tool,) = await toolset.get_tools(ctx)
            recorder.rejected_posts.add("Bearer rotated-1")
            with contextlib.suppress(Exception):
                await tool.run_async(args={}, tool_context=ctx)

            # The server accepts the token again (clock skew passed, say). It is
            # still cached, being young, and a new session carries it.
            recorder.rejected_posts.clear()
            result = await tool.run_async(args={}, tool_context=ctx)
    finally:
        recorder.rejected_posts.clear()
        await toolset.close()

    assert idp.calls == 1
    assert classify_tool_outcome(result) is ToolOutcome.SUCCESS


@pytest.mark.asyncio
async def test_a_401_on_the_optional_get_stream_leaves_the_session_working(http_server):
    url, recorder = http_server
    recorder.rejected_gets.add("Bearer rotated-1")
    idp = RotatingIdP()
    toolset = GuardedMcpToolset(_config(url=url, call_timeout_seconds=5), broker=_broker(idp))
    ctx = MagicMock()
    try:
        with _as("console-erin", "erin"):
            (tool,) = await toolset.get_tools(ctx)
            results = [await tool.run_async(args={}, tool_context=ctx) for _ in range(2)]
    finally:
        recorder.rejected_gets.clear()
        await toolset.close()

    assert all(classify_tool_outcome(r) is ToolOutcome.SUCCESS for r in results)
    assert idp.calls == 1


@pytest.mark.parametrize(
    ("transport", "method", "invalidates", "discards"),
    [
        ("streamable_http", "POST", True, True),
        ("streamable_http", "GET", False, False),
        ("streamable_http", "DELETE", False, False),
        ("sse", "POST", True, True),
        ("sse", "GET", True, False),
    ],
)
def test_what_a_401_does_depends_on_what_it_rejected(transport, method, invalidates, discards):
    toolset = GuardedMcpToolset(_config(transport=transport), broker=_broker())
    broker, manager = MagicMock(), MagicMock()
    toolset._broker, toolset._mcp_session_manager = broker, manager

    toolset._on_rejected_token("tok", method)

    assert broker.invalidate.called is invalidates
    assert toolset._rejection_count("tok") == (1 if invalidates else 0)
    assert manager._discard_session.called is discards


class RefusingAfterFirstIdP(RotatingIdP):
    def post(self, url: str, **kwargs: Any) -> Any:
        if self.calls >= 1:
            self.calls += 1
            refused = MagicMock(status_code=403)
            refused.json.return_value = {"error": "access_denied"}
            return refused
        return super().post(url, **kwargs)


@pytest.mark.asyncio
async def test_a_user_refused_after_a_rejection_is_answered_as_refused(http_server):
    url, recorder = http_server
    now = [0.0]
    settings = ExchangeSettings(url="https://idp.example.com/t", client_id="c", client_secret="s")
    idp = RefusingAfterFirstIdP()
    broker = TokenBroker(settings, session=cast(Any, idp), clock=lambda: now[0])
    toolset = GuardedMcpToolset(_config(url=url, call_timeout_seconds=5), broker=broker)
    ctx = MagicMock()
    try:
        with _as("console-frank", "frank"):
            (tool,) = await toolset.get_tools(ctx)
            recorder.rejected.add("Bearer rotated-1")
            now[0] += 60
            cut_off = await tool.run_async(args={}, tool_context=ctx)
            refused = await tool.run_async(args={}, tool_context=ctx)
    finally:
        recorder.rejected.clear()
        await toolset.close()

    assert cut_off["error_type"] == "credential_rejected"
    assert "check its effect" in cut_off["message"]
    assert refused["status"] == "access_denied"
    assert refused["error_type"] == "credential_unavailable"


@pytest.mark.asyncio
async def test_one_rejection_does_not_label_a_later_outage(http_server):
    url, recorder = http_server
    idp = RotatingIdP()
    toolset = GuardedMcpToolset(_config(url=url, call_timeout_seconds=5), broker=_broker(idp))
    ctx = MagicMock()
    try:
        with _as("console-grace", "grace"):
            (tool,) = await toolset.get_tools(ctx)
            recorder.rejected_posts.add("Bearer rotated-1")
            rejected = await tool.run_async(args={}, tool_context=ctx)
            recorder.rejected_posts.clear()
            recovered = await tool.run_async(args={}, tool_context=ctx)
            recorder.outage = True
            outage = await tool.run_async(args={}, tool_context=ctx)
    finally:
        recorder.rejected_posts.clear()
        recorder.outage = False
        await toolset.close()

    assert rejected["error_type"] == "credential_rejected"
    assert classify_tool_outcome(recovered) is ToolOutcome.SUCCESS
    assert "error_type" not in outage
    assert classify_tool_outcome(outage) is ToolOutcome.FAILURE


@pytest.mark.asyncio
async def test_a_refusal_during_the_call_is_answered_as_a_refusal():
    """The token went just before the call, and the header provider was refused."""
    refusal = CredentialUnavailable("refused", reason="refused")
    credential = AsyncMock(side_effect=["tok", refusal])
    tool = MagicMock(spec=["name", "run_async"])
    tool.name = "list_items"
    tool.run_async = AsyncMock(return_value={"error": "MCP tool execution failed: refused"})

    _with_deadline(tool, _config(), credential=credential)
    result = await tool.run_async(args={}, tool_context=MagicMock())

    assert result["status"] == "access_denied"
    assert result["error_type"] == "credential_unavailable"


@pytest.mark.asyncio
async def test_a_call_cut_off_by_a_closed_session_says_it_may_have_run(sse_server):
    """ADK's "execution failed" would invite a model to repeat a change that went through."""
    url, recorder = sse_server
    names = ["list_items", "slow_item"]
    toolset = GuardedMcpToolset(
        _config(auth=None, transport="sse", url=url, tools=names, read_only=names)
    )
    ctx = MagicMock()
    try:
        tools = {tool.name: tool for tool in await toolset.get_tools(ctx)}
        slow = asyncio.create_task(tools["slow_item"].run_async(args={}, tool_context=ctx))
        await asyncio.sleep(0.3)
        recorder.outage = True
        failed = await tools["list_items"].run_async(args={}, tool_context=ctx)
        recorder.outage = False
        cut_off = await slow
        after = await tools["list_items"].run_async(args={}, tool_context=ctx)
    finally:
        recorder.outage = False
        await toolset.close()

    for result in (failed, cut_off):
        assert result["status"] == "error"
        assert result["error_type"] == "session_closed"
        assert "may have run" in result["message"]
        assert classify_tool_outcome(result) is ToolOutcome.FAILURE
    assert classify_tool_outcome(after) is ToolOutcome.SUCCESS


@pytest.mark.asyncio
async def test_a_failure_with_no_closed_session_keeps_the_error():
    tool = MagicMock(spec=["name", "run_async"])
    tool.name = "list_items"
    bare = {"error": "MCP tool execution failed: boom"}
    tool.run_async = AsyncMock(return_value=bare)

    _with_deadline(tool, _config(auth=None))

    assert await tool.run_async(args={}, tool_context=MagicMock()) == bare


def test_a_closed_session_is_counted_for_its_own_user():
    toolset = GuardedMcpToolset(_config(transport="sse"), broker=_broker())
    manager = MagicMock(_sessions={"ann": object()})
    manager._discard_session.side_effect = lambda headers: manager._sessions.clear()
    toolset._mcp_session_manager = manager

    toolset._on_http_error(500, "POST", "ann")

    assert toolset._closed_session_count("ann") == 1
    assert toolset._closed_session_count("bob") == 0
    assert toolset._closed_session_count(None) == 0


def test_a_session_that_never_opened_is_not_counted_as_closed():
    """A POST failing during initialize: no call was cut off, so its error stands."""
    toolset = GuardedMcpToolset(_config(transport="sse"), broker=_broker())

    toolset._on_http_error(500, "POST", "ann")

    assert toolset._closed_session_count("ann") == 0


@pytest.mark.asyncio
async def test_a_redirected_post_closes_an_sse_session(sse_server):
    url, recorder = sse_server
    toolset = GuardedMcpToolset(
        _config(auth=None, transport="sse", url=url, call_timeout_seconds=2)
    )
    ctx = MagicMock()
    try:
        (tool,) = await toolset.get_tools(ctx)
        recorder.outage, recorder.outage_status = True, 302
        failed = await tool.run_async(args={}, tool_context=ctx)
        recorder.outage = False
        after = await tool.run_async(args={}, tool_context=ctx)
    finally:
        recorder.outage, recorder.outage_status = False, 500
        await toolset.close()

    assert failed["error_type"] == "session_closed"
    assert classify_tool_outcome(after) is ToolOutcome.SUCCESS


@pytest.mark.asyncio
async def test_a_redirect_the_sdk_follows_keeps_the_session(sse_server):
    url, recorder = sse_server
    toolset = GuardedMcpToolset(_config(auth=None, transport="sse", url=url))
    ctx = MagicMock()
    try:
        (tool,) = await toolset.get_tools(ctx)
        recorder.redirects = 1
        result = await tool.run_async(args={}, tool_context=ctx)
    finally:
        recorder.redirects = 0
        closed = toolset._closed_session_count(None)
        await toolset.close()

    assert classify_tool_outcome(result) is ToolOutcome.SUCCESS
    assert closed == 0


@pytest.mark.parametrize(
    ("status", "location", "follows"),
    [
        (307, "/messages/", True),
        (308, "http://127.0.0.1:1/other", True),
        (307, "https://elsewhere.example/x", False),
        (307, None, False),
        (307, "http://u:p@127.0.0.1:1/x", False),
        (302, "/messages/", False),
    ],
)
def test_only_a_redirect_the_sdk_follows_is_not_a_failure(status, location, follows):
    transport = GuardedTransport(url="http://127.0.0.1:1/sse", allow_private_network=True)
    headers = {"location": location} if location else {}
    response = httpx.Response(status, headers=headers)
    request = httpx.Request("POST", "http://127.0.0.1:1/messages")

    assert transport._sdk_follows(response, request) is follows


@pytest.mark.asyncio
async def test_a_post_with_no_response_closes_an_sse_session(sse_server, monkeypatch):
    url, _recorder = sse_server
    toolset = GuardedMcpToolset(
        _config(auth=None, transport="sse", url=url, call_timeout_seconds=2)
    )
    ctx = MagicMock()
    send = GuardedTransport._send
    resets = []

    async def resets_one_call(self: Any, request: Any) -> Any:
        if b"tools/call" in request.content and not resets:
            resets.append(request)
            raise httpx.ReadError("connection reset", request=request)
        return await send(self, request)

    monkeypatch.setattr(GuardedTransport, "_send", resets_one_call)
    try:
        (tool,) = await toolset.get_tools(ctx)
        failed = await tool.run_async(args={}, tool_context=ctx)
        after = await tool.run_async(args={}, tool_context=ctx)
    finally:
        await toolset.close()

    assert resets
    assert failed["error_type"] == "session_closed"
    assert classify_tool_outcome(after) is ToolOutcome.SUCCESS


@pytest.mark.asyncio
async def test_a_body_that_fails_after_the_headers_is_reported():
    """The SDK reads the body after the transport returned, so the stream has to say."""
    errors = []

    async def cut_off() -> Any:
        yield b"x"
        raise httpx.ReadError("connection reset")

    stream = _McpCappedStream(
        cut_off(), _ByteCap(1024, per_event=False), MagicMock(), lambda: errors.append("failed")
    )

    with pytest.raises(httpx.ReadError):
        async for _chunk in stream:
            pass

    assert errors == ["failed"]


@pytest.mark.asyncio
async def test_a_cancelled_post_reports_nothing():
    """Closing a session cancels its POST; that must not close its replacement."""
    errors = []
    transport = GuardedTransport(
        url="http://127.0.0.1:1/sse",
        allow_private_network=True,
        on_http_error=lambda *error: errors.append(error),
    )

    with (
        patch.object(GuardedTransport, "_send", AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await transport.handle_async_request(MagicMock())

    assert errors == []


def test_a_rejected_token_stays_out_of_the_shared_circuit():
    breaker = CircuitBreaker(failure_threshold=1)
    tool = MagicMock()
    tool.name = "inv_list_items"
    after = breaker.after_tool_callback()

    after(tool, {}, MagicMock(), {"status": "error", "error_type": "credential_rejected"})
    assert breaker.before_tool_callback()(tool, {}, MagicMock()) is None

    after(tool, {}, MagicMock(), {"error": "MCP tool execution failed: boom"})
    assert breaker.before_tool_callback()(tool, {}, MagicMock()) is not None


@pytest.mark.asyncio
async def test_a_static_sse_server_recovers_from_a_failed_post(sse_server):
    url, recorder = sse_server
    toolset = GuardedMcpToolset(
        _config(auth=None, transport="sse", url=url, call_timeout_seconds=2)
    )
    ctx = MagicMock()
    try:
        (tool,) = await toolset.get_tools(ctx)
        recorder.outage = True
        await tool.run_async(args={}, tool_context=ctx)
        recorder.outage = False
        result = await tool.run_async(args={}, tool_context=ctx)
    finally:
        recorder.outage = False
        await toolset.close()

    assert classify_tool_outcome(result) is ToolOutcome.SUCCESS
