"""``ORRERY_LONG_TERM_MEMORY=false``: durable chats, no lasting memory."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.load_memory_tool import LoadMemoryTool

from orrery_core import cross_session, default_plugins
from orrery_core.memory_switch import (
    LONG_TERM_MEMORY_ENV,
    long_term_memory_enabled,
    memory_safe_catalogue,
)
from orrery_core.plugins.memory_plugin import MemoryPlugin
from orrery_core.serving.gateway import AgentGateway


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setenv(LONG_TERM_MEMORY_ENV, "false")


def _root(*tools):
    root = MagicMock(tools=list(tools), sub_agents=[])
    root.name = "root"
    return root


def test_on_by_default(monkeypatch):
    monkeypatch.delenv(LONG_TERM_MEMORY_ENV, raising=False)
    assert long_term_memory_enabled()
    gateway = AgentGateway(
        app_name="t",
        root_agent=LlmAgent(name="root", model="gemini-test", tools=[LoadMemoryTool()]),
        session_service=MagicMock(),
        memory_service=MagicMock(),
        plugins=[MemoryPlugin()],
    )
    assert gateway.runner.memory_service is not None


def test_a_typo_fails_rather_than_reading_true(monkeypatch):
    monkeypatch.setenv(LONG_TERM_MEMORY_ENV, "flase")
    with pytest.raises(ValueError):
        long_term_memory_enabled()


def test_default_plugins_leave_memory_out(off):
    assert not any(isinstance(p, MemoryPlugin) for p in default_plugins(enable_memory=True))


def test_the_gateway_refuses_a_memory_service_or_plugin(off):
    with pytest.raises(ValueError, match=f"{LONG_TERM_MEMORY_ENV}=false.*memory"):
        AgentGateway(
            app_name="t",
            root_agent=_root(),
            session_service=MagicMock(),
            memory_service=MagicMock(),
        )
    with pytest.raises(ValueError, match="MemoryPlugin"):
        AgentGateway(
            app_name="t", root_agent=_root(), session_service=MagicMock(), plugins=[MemoryPlugin()]
        )


def test_the_gateway_refuses_recall_and_cross_session_tools(off):
    with pytest.raises(ValueError, match="load_memory"):
        AgentGateway(app_name="t", root_agent=_root(LoadMemoryTool()), session_service=MagicMock())

    @cross_session
    async def list_notes() -> dict:
        """Notes kept across sessions."""
        return {}

    journal = LlmAgent(name="journal", model="gemini-test", tools=[list_notes])
    root = LlmAgent(name="root", model="gemini-test", tools=[AgentTool(agent=journal)])
    with pytest.raises(ValueError, match="journal.list_notes"):
        AgentGateway(app_name="t", root_agent=root, session_service=MagicMock())


def test_a_tree_without_memory_is_served(off):
    root = LlmAgent(name="root", model="gemini-test")
    gateway = AgentGateway(app_name="t", root_agent=root, session_service=MagicMock())
    assert gateway.runner.memory_service is None


def test_a_wrapped_runner_is_checked_too(off):
    runner = MagicMock()
    runner.app.plugins = []
    runner.app.root_agent = _root()
    with pytest.raises(ValueError, match="memory"):
        AgentGateway.from_runner(runner)
    runner.memory_service = None
    assert AgentGateway.from_runner(runner).runner is runner


def test_a_catalogue_without_memory_drops_cross_session_tools(off):
    @cross_session
    async def list_notes() -> dict:
        """Notes kept across sessions."""
        return {}

    async def get_consumer_lag() -> dict:
        """Lag."""
        return {}

    catalogue = {"list_notes": list_notes, "get_consumer_lag": get_consumer_lag}
    assert set(memory_safe_catalogue(catalogue)) == {"get_consumer_lag"}


def test_a_catalogue_is_untouched_with_memory_on(monkeypatch):
    monkeypatch.delenv(LONG_TERM_MEMORY_ENV, raising=False)

    @cross_session
    async def list_notes() -> dict:
        """Notes kept across sessions."""
        return {}

    assert set(memory_safe_catalogue({"list_notes": list_notes})) == {"list_notes"}


def test_the_http_server_runs_without_memory():
    """The shipped front door, in a fresh interpreter, with the switch off."""
    probe = """
import gc
import orrery_assistant.app
from orrery_core.plugins.memory_plugin import MemoryPlugin
from orrery_core.serving.gateway import AgentGateway
[gateway] = [o for o in gc.get_objects() if isinstance(o, AgentGateway)]
plugins = gateway.runner.app.plugins
tools = {getattr(t, "name", "") for t in gateway.root_agent.tools}
print(f"MEMORY={gateway.runner.memory_service is not None} "
      f"PLUGIN={any(isinstance(p, MemoryPlugin) for p in plugins)} "
      f"RECALL={'load_memory' in tools} "
      f"JOURNAL={any('journal' in n for n in tools)}")
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORRERY_LEARNING")}
    env |= {"OTEL_TRACING_ENABLED": "false", "AUTH_ENABLED": "false", LONG_TERM_MEMORY_ENV: "false"}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "MEMORY=False PLUGIN=False RECALL=False JOURNAL=False" in result.stdout
