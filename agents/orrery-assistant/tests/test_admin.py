"""The chat root's admin builder: env is the ceiling, the catalog narrows and adds."""

from __future__ import annotations

import pytest

from orrery_assistant.admin import ChatAgentAdmin
from orrery_core import base_instruction, is_confirmation_gate
from orrery_core.agent.catalog_store import AgentCatalog
from orrery_core.agent.declarative import DeclarativeAgentConfig


def _delegates(root) -> list[str]:
    return [t.agent.name for t in root.tools if hasattr(t, "agent")]


def test_options_list_every_specialist_and_the_tool_catalogue():
    options = ChatAgentAdmin().options()

    assert {s.key for s in options.specialists} >= {"docker", "k8s", "kafka"}
    refs = {t.ref: t for t in options.tools}
    assert refs["get_consumer_lag"].guard == "read"
    assert refs["scale_deployment"].guard == "confirm"


def test_switching_a_specialist_off_removes_it_from_tools_and_routing():
    root = ChatAgentAdmin().build(AgentCatalog(disabled_specialists=["docker"]))

    assert "docker_agent" not in _delegates(root)
    assert "**docker_agent**" not in str(base_instruction(root))
    assert "kafka_health_agent" in _delegates(root)


def test_a_catalog_agent_is_added_and_gated_by_its_tools():
    agent = DeclarativeAgentConfig(
        name="scaler",
        description="Scales workloads when asked",
        instruction="Check the deployment, then scale it.",
        tools=["get_deployment_status", "scale_deployment"],
    )
    root = ChatAgentAdmin().build(AgentCatalog(agents=[agent]))

    (scaler,) = [
        t.agent for t in root.tools if getattr(t, "agent", None) and t.agent.name == "scaler"
    ]
    assert is_confirmation_gate(scaler.before_tool_callback)
    assert "**scaler**: Scales workloads when asked" in str(base_instruction(root))


@pytest.mark.parametrize(
    ("catalog", "message"),
    [
        (AgentCatalog(disabled_specialists=["nope"]), "does not run"),
        (
            AgentCatalog(
                agents=[
                    DeclarativeAgentConfig(
                        name="incident_triage_agent",
                        description="Shadow the triage agent",
                        instruction="Do the triage.",
                        tools=["list_pods"],
                    )
                ]
            ),
            "collides",
        ),
    ],
)
def test_invalid_catalogs_are_refused(catalog, message):
    with pytest.raises(ValueError, match=message):
        ChatAgentAdmin().build(catalog)


def _tool_names(agent) -> set[str]:
    return {getattr(t, "name", None) or getattr(t, "__name__", "") for t in agent.tools}


def _triage(root):
    (triage,) = [
        t.agent
        for t in root.tools
        if getattr(t, "agent", None) and t.agent.name == "incident_triage_agent"
    ]
    return triage


def test_switching_a_specialist_off_keeps_its_tools_out_of_the_sweep():
    # The sweep used to be a fixed list: a "full health sweep" still called
    # Docker tools on a deployment that had switched Docker off.
    root = ChatAgentAdmin().build(AgentCatalog(disabled_specialists=["docker", "k8s"]))
    tools = _tool_names(_triage(root))
    assert {"list_containers", "get_container_stats", "docker_compose_status"}.isdisjoint(tools)
    assert {"list_pods", "get_nodes", "get_events", "get_cluster_info"}.isdisjoint(tools)
    assert {"get_consumer_lag", "get_prometheus_alerts", "record_triage_verdict"} <= tools
    instruction = str(base_instruction(_triage(root)))
    assert "Docker" not in instruction and "Kubernetes" not in instruction


def test_no_sweep_when_no_sweepable_system_is_on():
    root = ChatAgentAdmin().build(
        AgentCatalog(
            disabled_specialists=["docker", "k8s", "kafka", "observability", "elasticsearch"]
        )
    )
    assert "incident_triage_agent" not in _delegates(root)
    assert "incident_triage_agent" not in str(base_instruction(root))


def test_the_journal_is_only_offered_while_it_is_on():
    # The offer to save findings was fixed text: with the journal switched off,
    # the coordinator still asked "want me to save this to your ops journal?".
    offer = "offer once to save findings"
    on = ChatAgentAdmin().build(AgentCatalog())
    assert "ops_journal_agent" in _delegates(on)
    assert offer in str(base_instruction(on))

    off = ChatAgentAdmin().build(AgentCatalog(disabled_specialists=["ops_journal"]))
    assert "ops_journal_agent" not in _delegates(off)
    assert offer not in str(base_instruction(off))
    assert "ops_journal" not in str(base_instruction(off))
