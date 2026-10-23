"""Per-user MCP credentials (ADR-004) against a real streamable-HTTP server."""

from __future__ import annotations

import socket
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

pytest.importorskip("mcp")

import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from orrery_core.reliability.resilience import ToolOutcome, classify_tool_outcome  # noqa: E402
from orrery_core.security.identity import CallerIdentity, caller_scope  # noqa: E402
from orrery_core.security.token_exchange import (  # noqa: E402
    CredentialUnavailable,
    ExchangeSettings,
    TokenBroker,
)
from orrery_core.tools.mcp import GuardedMcpToolset, McpServerConfig  # noqa: E402


class Recorder:
    """ASGI middleware: which Authorization each MCP session carried."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.by_session: dict[str, set[str]] = defaultdict(set)
        self.unauthenticated = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization")
        if auth is None:
            self.unauthenticated += 1

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
        transient = False

        async def token_for(self, audience: str, scope: str | None = None) -> str:
            raise CredentialUnavailable("the IdP refused", transient=self.transient)

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
            broker.transient = True
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
