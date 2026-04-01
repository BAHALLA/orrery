"""Tests for ADK Plugins (core/ai_agents_core/plugins.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ai_agents_core.plugins import (
    ActivityPlugin,
    AuditPlugin,
    ErrorHandlerPlugin,
    GuardrailsPlugin,
    MetricsPlugin,
    ResiliencePlugin,
    default_plugins,
)
from ai_agents_core.rbac import Role, RolePolicy

# Fixtures for ADK mock objects


@pytest.fixture
def base_tool():
    tool = MagicMock()
    tool.name = "my_tool"
    # Mock the underlying function to handle guardrail checks
    tool.func = lambda: None
    return tool


@pytest.fixture
def tool_context():
    ctx = MagicMock()
    ctx.state = {}
    ctx.agent_name = "test_agent"
    return ctx


@pytest.fixture
def callback_context():
    ctx = MagicMock()
    ctx.state = {}
    return ctx


@pytest.fixture
def base_agent():
    agent = MagicMock()
    agent.name = "test_agent"
    return agent


# ── GuardrailsPlugin Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_guardrails_plugin_rbac_blocks(base_tool, tool_context):
    """Verify RBAC check blocks unauthorized tool calls."""
    policy = RolePolicy(overrides={"my_tool": Role.ADMIN}, default_role=Role.VIEWER)
    plugin = GuardrailsPlugin(role_policy=policy)

    # Mock context with viewer role (not authorized for "my_tool")
    tool_context.state["user_role"] = "viewer"

    result = await plugin.before_tool_callback(tool=base_tool, args={}, tool_context=tool_context)

    assert result is not None
    assert result["status"] == "access_denied"
    assert "Access denied" in result["message"]


@pytest.mark.asyncio
async def test_guardrails_plugin_confirm_mode_skips_gate(base_tool, tool_context):
    """In confirm mode, GuardrailsPlugin delegates confirmation to ADK's native
    FunctionTool(require_confirmation=True) — no plugin-level gate."""
    from ai_agents_core.guardrails import confirm

    @confirm("testing")
    def my_guarded_func():
        pass

    base_tool.func = my_guarded_func
    base_tool.name = "my_tool"

    plugin = GuardrailsPlugin(mode="confirm")
    tool_context.state["user_role"] = "admin"  # Authorized

    result = await plugin.before_tool_callback(tool=base_tool, args={}, tool_context=tool_context)

    # No confirmation gate — RBAC passes, tool proceeds
    assert result is None


@pytest.mark.asyncio
async def test_guardrails_plugin_dry_run_blocks(base_tool, tool_context):
    """Verify dry_run mode still blocks guarded tools at the plugin level."""
    from ai_agents_core.guardrails import destructive

    @destructive("deletes data")
    def my_destructive_func():
        pass

    base_tool.func = my_destructive_func
    base_tool.name = "my_tool"

    plugin = GuardrailsPlugin(mode="dry_run")
    tool_context.state["user_role"] = "admin"

    result = await plugin.before_tool_callback(tool=base_tool, args={}, tool_context=tool_context)

    assert result is not None
    assert result["status"] == "dry_run"


# ── ResiliencePlugin Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_resilience_plugin_opens_circuit(base_tool, tool_context):
    """Verify ResiliencePlugin blocks calls when circuit is open."""
    plugin = ResiliencePlugin(failure_threshold=1)

    # Trigger a failure to open circuit
    await plugin.on_tool_error_callback(
        tool=base_tool, tool_args={}, tool_context=tool_context, error=Exception("Fail")
    )

    # Next call should be blocked by before_tool_callback
    result = await plugin.before_tool_callback(tool=base_tool, args={}, tool_context=tool_context)

    assert result is not None
    assert "temporarily unavailable" in result["message"]


# ── MetricsPlugin Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_plugin_tracks_calls(base_tool, tool_context):
    """Verify MetricsPlugin calls the underlying collector."""
    plugin = MetricsPlugin()

    with patch.object(plugin, "_before", wraps=plugin._before) as mock_before:
        await plugin.before_tool_callback(tool=base_tool, args={}, tool_context=tool_context)
        mock_before.assert_called_once()


# ── AuditPlugin Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_plugin_logs_call(base_tool, tool_context):
    """Verify AuditPlugin calls the audit logger."""
    plugin = AuditPlugin()

    with patch.object(plugin, "_callback", wraps=plugin._callback) as mock_audit:
        await plugin.after_tool_callback(
            tool=base_tool, args={}, tool_context=tool_context, tool_response={"status": "success"}
        )
        mock_audit.assert_called_once()


# ── ActivityPlugin Tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activity_plugin_updates_state(base_tool, tool_context):
    """Verify ActivityPlugin updates session activity log."""
    plugin = ActivityPlugin()

    await plugin.after_tool_callback(
        tool=base_tool, args={}, tool_context=tool_context, tool_response={"status": "success"}
    )

    assert "session_log" in tool_context.state
    assert len(tool_context.state["session_log"]) == 1
    assert tool_context.state["session_log"][0]["operation"] == "my_tool"


# ── ErrorHandlerPlugin Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_handler_plugin_suppresses_tool_error(base_tool, tool_context):
    """Verify ErrorHandlerPlugin returns a structured error dict."""
    plugin = ErrorHandlerPlugin()

    result = await plugin.on_tool_error_callback(
        tool=base_tool, tool_args={}, tool_context=tool_context, error=ValueError("Invalid arg")
    )

    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert "Invalid arg" in result["message"]


@pytest.mark.asyncio
async def test_error_handler_plugin_suppresses_model_error(callback_context):
    """Verify ErrorHandlerPlugin returns a LlmResponse on model error."""
    plugin = ErrorHandlerPlugin()
    llm_request = MagicMock()

    result = await plugin.on_model_error_callback(
        callback_context=callback_context, llm_request=llm_request, error=Exception("Model timeout")
    )

    from google.adk.models.llm_response import LlmResponse

    assert isinstance(result, LlmResponse)
    assert "Model timeout" in result.content.parts[0].text


# ── Factory Tests ────────────────────────────────────────────────────


def test_default_plugins_composition():
    """Verify default_plugins returns the expected list and order."""
    plugins = default_plugins()

    expected_names = ["guardrails", "resilience", "metrics", "audit", "activity", "error_handler"]

    # ADK BasePlugin has a .name property
    plugin_names = [p.name for p in plugins]
    assert plugin_names == expected_names

    # Verify ErrorHandlerPlugin is last
    assert isinstance(plugins[-1], ErrorHandlerPlugin)
