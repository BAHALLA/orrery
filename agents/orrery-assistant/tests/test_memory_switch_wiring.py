"""``ORRERY_LONG_TERM_MEMORY=false`` against the coordinator's real builders.

The admin catalog is the path that matters most: an edit is built, then saved,
then swapped in, so a tree the gateway would refuse must already fail the
build, or a saved catalog keeps the deployment from starting.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orrery_assistant.admin import ChatAgentAdmin
from orrery_core.agent.catalog_store import AgentCatalog
from orrery_core.agent.declarative import DeclarativeAgentConfig
from orrery_core.memory_switch import LONG_TERM_MEMORY_ENV
from orrery_core.serving.gateway import AgentGateway


def _note_taker() -> DeclarativeAgentConfig:
    return DeclarativeAgentConfig(
        name="note_taker",
        description="keeps incident notes for the team",
        instruction="save what the user asks you to save",
        tools=["list_notes"],
    )


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setenv(LONG_TERM_MEMORY_ENV, "false")


def test_an_admin_edit_holding_journal_tools_fails_the_build(off):
    with pytest.raises(ValueError, match=r"note_taker\.list_notes"):
        ChatAgentAdmin().build(AgentCatalog(agents=[_note_taker()]))


def test_the_agents_tab_does_not_offer_journal_tools(off):
    refs = {t.ref for t in ChatAgentAdmin().options().tools}
    assert "list_notes" not in refs and "save_note" not in refs
    assert "get_consumer_lag" in refs


def test_with_memory_on_the_same_edit_builds(monkeypatch):
    monkeypatch.delenv(LONG_TERM_MEMORY_ENV, raising=False)
    ChatAgentAdmin().build(AgentCatalog(agents=[_note_taker()]))
    assert "list_notes" in {t.ref for t in ChatAgentAdmin().options().tools}


def test_an_edited_tree_without_journal_tools_is_served(off):
    root = ChatAgentAdmin().build(AgentCatalog(disabled_specialists=["docker"]))
    AgentGateway(app_name="t", root_agent=root, session_service=MagicMock())


def test_a_workflow_root_is_walked(off):
    """The triage Workflow's journal step is reached through its graph nodes."""
    from orrery_assistant.agent import journal_writer, orrery_triage_workflow

    if journal_writer is None:
        pytest.skip("no journal specialist enabled here")
    with pytest.raises(ValueError, match=r"journal_writer\."):
        AgentGateway(app_name="t", root_agent=orrery_triage_workflow, session_service=MagicMock())
