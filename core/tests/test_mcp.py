"""Guarded MCP servers: config validation, fail-closed classification, gates.

The integration tests run a real MCP server over stdio
(``fixtures/mcp_inventory_server.py``), so they exercise ADK's ``McpToolset``
and the MCP SDK rather than a mock of either.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from orrery_core import (  # noqa: E402
    get_guard_level,
    infer_minimum_role,
    is_confirmation_gate,
    require_confirmation,
)
from orrery_core.security.rbac import Role  # noqa: E402
from orrery_core.serving.gateway import AgentGateway, InboundMessage  # noqa: E402
from orrery_core.tools.mcp import (  # noqa: E402
    GuardedMcpToolset,
    McpConfig,
    McpServerConfig,
    classify_mcp_tool,
    create_mcp_agent,
    load_mcp_config,
)

from .conftest import FakeState, FakeToolContext  # noqa: E402

SERVER = Path(__file__).parent / "fixtures" / "mcp_inventory_server.py"


def _config(**over: Any) -> McpServerConfig:
    base: dict[str, Any] = {
        "name": "inv",
        "description": "Inventory service",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(SERVER)],
        "tools": ["list_items", "delete_item", "update_item", "purge_cache", "drop_table"],
        "read_only": ["list_items"],
    }
    return McpServerConfig(**{**base, **over})


# ── Config validation ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"tools": []}, "at least 1"),
        ({"read_only": ["nope"]}, "missing from `tools`"),
        ({"destructive": ["list_items"]}, "both read_only and destructive"),
        ({"name": "Bad-Name"}, "must match"),
        ({"command": None}, "needs `command`"),
        ({"url": "https://x.example"}, "takes no `url`"),
    ],
)
def test_invalid_server_config_is_rejected(over, message):
    with pytest.raises(ValidationError, match=message):
        _config(**over)


def test_plain_http_is_refused_except_on_loopback():
    http = {"transport": "streamable_http", "command": None, "args": []}
    with pytest.raises(ValidationError, match="use https"):
        _config(**http, url="http://mcp.internal.example/mcp")

    _config(**http, url="http://127.0.0.1:9000/mcp")  # loopback is fine
    _config(**http, url="http://mcp.internal.example/mcp", allow_insecure_http=True)


def test_duplicate_server_names_are_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        McpConfig(servers=[_config(), _config()])


def test_missing_header_secret_fails_at_construction(monkeypatch):
    monkeypatch.delenv("INV_TOKEN", raising=False)
    cfg = _config(
        transport="streamable_http",
        command=None,
        args=[],
        url="https://mcp.example/mcp",
        headers_from_env={"Authorization": "INV_TOKEN"},
    )

    with pytest.raises(ValueError, match="INV_TOKEN is not set"):
        GuardedMcpToolset(cfg)


def test_http_server_gets_its_headers_from_the_environment(monkeypatch):
    monkeypatch.setenv("INV_TOKEN", "Bearer s3cret")
    cfg = _config(
        transport="streamable_http",
        command=None,
        args=[],
        url="https://mcp.example/mcp",
        headers_from_env={"Authorization": "INV_TOKEN"},
    )

    from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

    params = GuardedMcpToolset(cfg)._connection_params

    assert isinstance(params, StreamableHTTPConnectionParams)
    assert params.url == "https://mcp.example/mcp"
    assert params.headers == {"Authorization": "Bearer s3cret"}


def test_no_config_file_means_no_servers(monkeypatch):
    monkeypatch.delenv("ORRERY_MCP_CONFIG", raising=False)

    assert load_mcp_config().servers == []


def test_config_file_round_trip(tmp_path):
    path = tmp_path / "mcp.yaml"
    path.write_text(
        "servers:\n"
        "  - name: inv\n"
        "    description: Inventory\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    tools: [list_items]\n"
        "    read_only: [list_items]\n"
    )

    assert [s.name for s in load_mcp_config(path).servers] == ["inv"]


def test_a_configured_but_missing_file_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_mcp_config(tmp_path / "absent.yaml")


# ── Classification ───────────────────────────────────────────────────


class _Ann:
    def __init__(self, **hints: bool) -> None:
        for k, v in hints.items():
            setattr(self, k, v)


@pytest.mark.parametrize(
    ("name", "annotations", "trust", "expected"),
    [
        # Operator decisions win.
        ("list_items", None, False, None),
        # Undeclared, no hints: the MCP default (destructiveHint=true).
        ("purge_cache", None, False, "destructive"),
        # A hint that *raises* protection is always honoured…
        ("delete_item", _Ann(destructive_hint=True), False, "destructive"),
        # …one that lowers it only with trust_annotations…
        ("update_item", _Ann(destructive_hint=False), False, "destructive"),
        ("update_item", _Ann(destructive_hint=False), True, "confirm"),
        ("peek", _Ann(read_only_hint=True), True, None),
        # …and never for a name that reads as a mutation.
        ("drop_table", _Ann(read_only_hint=True), True, "destructive"),
        # mcp 1.x spelling is read too.
        ("peek", _Ann(readOnlyHint=True), True, None),
    ],
)
def test_classification_fails_closed(name, annotations, trust, expected):
    cfg = _config(
        tools=[name],
        read_only=["list_items"] if name == "list_items" else [],
        trust_annotations=trust,
    )

    level, _ = classify_mcp_tool(name, annotations, cfg)

    assert level == expected


# ── A real MCP server over stdio ─────────────────────────────────────


@pytest.fixture
def marker(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "calls.txt"
    monkeypatch.setenv("MCP_MARKER", str(path))
    return path


def _marker_config(**over: Any) -> McpServerConfig:
    return _config(env_from={"MCP_MARKER": "MCP_MARKER"}, **over)


@pytest.mark.asyncio
async def test_toolset_exposes_only_the_allowlist_with_guard_levels(marker):
    toolset = GuardedMcpToolset(_marker_config(trust_annotations=True))
    try:
        tools = {t.name: t for t in await toolset.get_tools()}
    finally:
        await toolset.close()

    assert "secret_tool" not in tools
    assert {name: get_guard_level(t) for name, t in tools.items()} == {
        "list_items": None,  # declared read_only
        "delete_item": "destructive",  # server hint raises protection
        "update_item": "confirm",  # trusted non-destructive hint
        "purge_cache": "destructive",  # no annotations: MCP default
        "drop_table": "destructive",  # claims read-only, name says otherwise
    }
    # The labels feed RBAC like a decorator would.
    assert infer_minimum_role(tools["delete_item"]) is Role.ADMIN
    assert infer_minimum_role(tools["list_items"]) is Role.VIEWER


@pytest.mark.asyncio
async def test_confirmation_gate_stops_an_mcp_tool_without_a_python_function(marker):
    """Regression: gates used to return early for tools with no `func`."""
    toolset = GuardedMcpToolset(_marker_config())
    try:
        delete = next(t for t in await toolset.get_tools() if t.name == "delete_item")
    finally:
        await toolset.close()
    ctx = FakeToolContext(state=FakeState())

    answer = require_confirmation()(tool=delete, args={"item": "widget"}, tool_context=ctx)

    assert answer is not None and answer["status"] == "confirmation_required"


class _ScriptedLlm(BaseLlm):
    model: str = "scripted"
    script: list[LlmResponse] = []

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        yield self.script.pop(0)


@pytest.mark.asyncio
async def test_mcp_specialist_never_reaches_the_server_without_approval(marker):
    """End to end: a model asks for a destructive MCP call; nothing happens."""
    call = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_function_call(name="inv_delete_item", args={"item": "widget"})],
        )
    )
    done = LlmResponse(content=types.Content(role="model", parts=[types.Part.from_text(text="ok")]))
    agent = create_mcp_agent(_marker_config(), model=_ScriptedLlm(script=[call, done]))
    assert is_confirmation_gate(agent.before_tool_callback)

    gateway = AgentGateway(
        app_name="t", root_agent=agent, plugins=[], session_service=InMemorySessionService()
    )
    responses: dict[str, Any] = {}

    async def on_event(event: Any) -> None:
        for part in (event.content.parts if event.content else None) or []:
            if part.function_response:
                responses[part.function_response.name] = part.function_response.response

    try:
        await gateway.run(
            InboundMessage(text="delete the widget", user_id="u", conversation_key="", channel="t"),
            on_event=on_event,
        )
    finally:
        for toolset in agent.tools:
            if isinstance(toolset, GuardedMcpToolset):
                await toolset.close()

    # The call was made and the gate answered it — not merely "nothing ran",
    # which a misnamed tool would satisfy just as well.
    assert responses["inv_delete_item"]["status"] == "confirmation_required"
    assert not marker.exists() or "delete_item" not in marker.read_text()
