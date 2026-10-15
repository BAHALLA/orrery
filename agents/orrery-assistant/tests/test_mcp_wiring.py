"""MCP specialists on the chat root: opt-in, and gated like every other agent."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from google.adk.agents import LlmAgent

pytest.importorskip("mcp")

from orrery_assistant.agent import _mcp_specialists  # noqa: E402
from orrery_core import is_confirmation_gate  # noqa: E402
from orrery_core.tools.mcp import GuardedMcpToolset  # noqa: E402

FIXTURE = Path(__file__).parents[3] / "core" / "tests" / "fixtures" / "mcp_inventory_server.py"


def test_no_config_means_no_mcp_specialists(monkeypatch):
    monkeypatch.delenv("ORRERY_MCP_CONFIG", raising=False)

    assert _mcp_specialists() == []


def test_configured_server_becomes_a_gated_specialist(tmp_path, monkeypatch):
    config = tmp_path / "mcp.yaml"
    config.write_text(
        "servers:\n"
        "  - name: inv\n"
        "    description: Inventory service\n"
        "    transport: stdio\n"
        f"    command: {sys.executable}\n"
        f"    args: [{str(FIXTURE)!r}]\n"
        "    tools: [list_items, delete_item]\n"
        "    read_only: [list_items]\n"
    )
    monkeypatch.setenv("ORRERY_MCP_CONFIG", str(config))

    (specialist,) = _mcp_specialists()

    agent = specialist.agent
    assert isinstance(agent, LlmAgent)
    assert agent.name == "inv_mcp_agent"
    assert any(isinstance(t, GuardedMcpToolset) for t in agent.tools)
    # The toolset's tools are unknown until run time, so the gate is
    # attached unconditionally (create_agent fails closed on toolsets).
    assert is_confirmation_gate(agent.before_tool_callback)


def test_a_broken_config_fails_startup(tmp_path, monkeypatch):
    config = tmp_path / "mcp.yaml"
    config.write_text("servers:\n  - name: inv\n    transport: stdio\n")
    monkeypatch.setenv("ORRERY_MCP_CONFIG", str(config))

    with pytest.raises(ValueError):
        _mcp_specialists()
