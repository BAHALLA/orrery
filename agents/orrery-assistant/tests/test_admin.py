"""The chat root's admin builder: env is the ceiling, the catalog narrows and adds."""

from __future__ import annotations

import pytest
from google.adk.agents import LlmAgent

from orrery_assistant import agent as chat
from orrery_assistant.admin import ChatAgentAdmin
from orrery_core import base_instruction, confirm, is_confirmation_gate
from orrery_core.agent.catalog_store import AgentCatalog
from orrery_core.agent.declarative import DeclarativeAgentConfig
from orrery_core.registry import HealthCheck, Installed, Journal, Remediation, Specialist


def _delegates(root) -> list[str]:
    return [t.agent.name for t in root.tools if hasattr(t, "agent")]


def test_options_list_every_specialist_and_the_tool_catalogue():
    options = ChatAgentAdmin().options()

    assert {s.key for s in options.specialists} >= {"docker", "k8s", "kafka"}
    refs = {t.ref: t for t in options.tools}
    assert refs["get_consumer_lag"].guard == "read"
    assert refs["scale_deployment"].guard == "confirm"
    # What build() refuses as a composed agent's name, so the console can say so first.
    assert {"orrery_chat_agent", "load_memory"} <= set(options.reserved_names)


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


@pytest.mark.parametrize("name", ["load_memory", "search_knowledge", "orrery_chat_agent"])
def test_a_catalog_agent_may_not_take_a_coordinator_tool_name(name):
    # It built, so it was saved and swapped in, and every turn then failed at
    # the model API on two function declarations with one name.
    catalog = AgentCatalog(
        agents=[
            DeclarativeAgentConfig(
                name=name,
                description="Shadows a tool the coordinator already has",
                instruction="Check events first.",
                tools=["get_events"],
            )
        ]
    )
    with pytest.raises(ValueError, match="collides"):
        ChatAgentAdmin().build(catalog)


def test_the_coordinator_refuses_two_tools_with_one_name():
    shadow = LlmAgent(name="kafka_health_agent", model="gemini-x", description="Shadow.")
    with pytest.raises(ValueError, match="two different tools are both named 'kafka_health_agent'"):
        chat.build_chat_agent(chat._specialists, [shadow])


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
    # Elasticsearch's rubric mentions ECK on Kubernetes; the systems swept are
    # the per-system headings.
    assert "**Docker**" not in instruction and "**Kubernetes**" not in instruction
    assert "**Kafka**" in instruction


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


# ── A third-party specialist is a first-class citizen ────────────────


async def redis_ping() -> dict:
    """Ping Redis."""
    return {}


async def redis_memory() -> dict:
    """Read Redis memory usage."""
    return {}


@confirm("fails the Redis primary over to a replica")
async def failover_redis() -> dict:
    """Fail over."""
    return {}


async def write_runbook_note() -> dict:
    """Save a note in the team wiki."""
    return {}


async def record_change() -> dict:
    """Record an operation in the change log."""
    return {}


def _redis(**extra) -> Specialist:
    agent = LlmAgent(
        name="redis_agent",
        model="gemini-x",
        description="Redis.",
        tools=[redis_ping, redis_memory, failover_redis, write_runbook_note, record_change],
    )
    return Specialist(
        key="redis",
        agent=agent,
        distribution="acme-redis-agent",
        routing_hint="Redis health, memory, keyspace, replication.",
        health_check=HealthCheck(
            system="Redis",
            checks="ping and memory usage.",
            criteria="critical = no PONG; degraded = memory above 90%; healthy = none.",
            tools=(redis_ping, redis_memory),
        ),
        **extra,
    )


def test_a_plugin_specialist_routes_and_sweeps_like_a_builtin():
    # Nothing in the coordinator names Redis: everything below comes from the
    # specialist's own declaration.
    root = chat.build_chat_agent([_redis()], [])
    instruction = str(base_instruction(root))
    assert "redis_agent" in _delegates(root)
    assert "- **redis_agent**: Redis health, memory, keyspace, replication." in instruction

    sweep = _triage(root)
    assert {"redis_ping", "redis_memory", "record_triage_verdict"} == _tool_names(sweep)
    sweep_instruction = str(base_instruction(sweep))
    assert "**Redis**" in sweep_instruction and "memory above 90%" in sweep_instruction

    checker = chat.build_health_checker(_redis())
    assert checker.name == "redis_health_checker" and checker.output_key == "redis_status"


def test_a_plugin_journal_is_offered_under_its_own_name():
    offer = "offer once to save findings via redis_agent"
    assert offer not in str(base_instruction(chat.build_chat_agent([_redis()], [])))
    with_journal = chat.build_chat_agent([_redis(journal=_REDIS_JOURNAL)], [])
    assert offer in str(base_instruction(with_journal))


_REDIS_JOURNAL = Journal(save=write_runbook_note, log=record_change)
_REDIS_REMEDIATION = Remediation(
    actions=(failover_redis,), playbook="failover_redis when the primary is down."
)


def test_a_plugin_journal_saves_the_batch_report_with_its_own_tools():
    # The batch step used to call ops-journal's tools whatever journal was on.
    writer = chat.build_journal_writer(_redis(journal=_REDIS_JOURNAL))
    assert _tool_names(writer) == {"write_runbook_note", "record_change"}
    assert "save_note" not in str(base_instruction(writer))
    assert "with write_runbook_note" in str(base_instruction(writer))


def test_a_plugin_remediation_is_the_only_one_the_loop_can_take():
    from orrery_assistant.remediation import build_remediation

    nodes = build_remediation([_redis(remediation=_REDIS_REMEDIATION)], record_change)
    assert nodes is not None
    assert _tool_names(nodes.actor) == {"failover_redis", "record_change"}
    assert is_confirmation_gate(nodes.actor.before_tool_callback)
    assert "failover_redis when the primary is down" in str(base_instruction(nodes.actor))
    # The verifier sees the system through its health-check reads.
    assert _tool_names(nodes.verifier) == {
        "redis_ping",
        "redis_memory",
        "mark_remediation_resolved",
    }
    assert _tool_names(nodes.summarizer) == {"record_change"}


def test_no_remediation_loop_without_a_remediating_specialist():
    # The lab runs with k8s off: Kafka and observability only observe, so an
    # unattended run must have nothing it could act with.
    from orrery_assistant.remediation import build_remediation

    assert build_remediation([_redis()]) is None
    kafka_only = [s for s in chat._specialists if s.key in ("kafka", "observability")]
    assert build_remediation(kafka_only) is None


def test_the_k8s_remediation_comes_from_its_declaration():
    from orrery_assistant.remediation import build_remediation

    nodes = build_remediation([s for s in chat._specialists if s.key == "k8s"])
    assert nodes is not None
    assert {"restart_deployment", "scale_deployment", "rollback_deployment"} == _tool_names(
        nodes.actor
    )


def test_two_tools_with_one_name_in_a_sweep_are_refused():
    # Two function declarations with one name are a 400 from the model API.
    async def get_events() -> dict:
        """Another system's events."""
        return {}

    clash = Specialist(
        key="other",
        agent=LlmAgent(name="other_agent", model="gemini-x", tools=[get_events]),
        distribution="acme-other",
        health_check=HealthCheck(system="Other", checks=".", criteria=".", tools=(get_events,)),
    )
    k8s = [s for s in chat._specialists if s.key == "k8s"]
    with pytest.raises(ValueError, match="two different tools are both named 'get_events'"):
        chat.build_triage_agent([*k8s, clash])


def test_options_list_excluded_specialists_without_importing_them(monkeypatch):
    from orrery_assistant import admin

    installed = [*admin.installed_specialists(), Installed("billing", "acme-billing", "plugin")]
    monkeypatch.setattr(admin, "installed_specialists", lambda: installed)
    billing = next(s for s in ChatAgentAdmin().options().specialists if s.key == "billing")
    assert not billing.available and billing.source == "plugin" and billing.agent_name == ""


def test_the_console_lists_the_real_coordinators_specialists_not_its_helpers():
    """What /me sends the console, against the shipped tree: the triage sweep
    agent is an AgentTool too, but it is not a system to ask about."""
    from orrery_core.registry import key_of
    from orrery_core.serving.inventory import specialist_entries

    entries = specialist_entries(chat.orrery_chat_agent)
    names = {e.name for e in entries}

    assert "incident_triage_agent" not in names
    assert {"kafka_health_agent", "k8s_health_agent"} <= names
    assert key_of("kafka_health_agent") == "kafka"
