"""Specialist registry (entry points) and declarative agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from pydantic import ValidationError

from orrery_core import confirm, is_confirmation_gate
from orrery_core.agent.declarative import (
    DeclarativeAgentsFile,
    build_declarative_agents,
    load_agents_config,
    tool_catalogue,
)
from orrery_core.registry import (
    JOURNAL,
    HealthCheck,
    Specialist,
    SpecialistSpec,
    discover_specialists,
    source_of,
)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("ORRERY_SPECIALISTS", raising=False)
    monkeypatch.delenv("ORRERY_DISABLED_SPECIALISTS", raising=False)


async def get_status() -> dict:
    """Read status."""
    return {}


async def get_events() -> dict:
    """Read events."""
    return {}


@confirm("restarts a service")
async def restart_service() -> dict:
    """Restart."""
    return {}


def _agent(name: str, tools: list[Any]) -> LlmAgent:
    return LlmAgent(name=name, model="gemini-x", tools=tools)


@dataclass
class _Dist:
    name: str


@dataclass
class _EP:
    name: str
    obj: Any
    dist: _Dist
    value: str = "pkg.agent:root_agent"

    def load(self) -> Any:
        return self.obj


def _eps(*pairs: tuple[str, Any], dist: str = "kafka-health-agent"):
    return lambda: [_EP(k, v, _Dist(dist)) for k, v in pairs]


# ── Registry ─────────────────────────────────────────────────────────


def test_discovers_every_installed_specialist_sorted():
    loader = _eps(("kafka", _agent("kafka_a", [])), ("docker", _agent("docker_a", [])))

    assert [s.key for s in discover_specialists(loader=loader)] == ["docker", "kafka"]


def test_enable_and_disable_lists(monkeypatch):
    loader = _eps(("kafka", _agent("kafka_a", [])), ("docker", _agent("docker_a", [])))

    monkeypatch.setenv("ORRERY_SPECIALISTS", "kafka")
    assert [s.key for s in discover_specialists(loader=loader)] == ["kafka"]

    monkeypatch.delenv("ORRERY_SPECIALISTS")
    monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", "kafka")
    assert [s.key for s in discover_specialists(loader=loader)] == ["docker"]


def test_a_typo_in_the_lists_stops_startup(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", "kafak")

    with pytest.raises(ValueError, match="not installed: \\['kafak'\\]"):
        discover_specialists(loader=_eps(("kafka", _agent("kafka_a", []))))


def test_an_entry_point_that_is_not_an_agent_is_rejected():
    with pytest.raises(ValueError, match="not an agent"):
        discover_specialists(loader=_eps(("bad", object())))


def test_third_party_distributions_are_recorded_as_plugins():
    discover_specialists(loader=_eps(("billing", _agent("billing_a", [])), dist="acme_billing"))
    discover_specialists(loader=_eps(("kafka", _agent("kafka_b", [])), dist="kafka_health_agent"))

    assert source_of("billing_a") == "plugin"
    assert source_of("kafka_b") == "builtin"  # normalised: kafka_health_agent == kafka-health-agent


def test_the_real_workspace_registers_its_specialists(monkeypatch):
    """The installed entry points themselves — what the chat root uses."""
    keys = [s.key for s in discover_specialists(record=False)]
    assert {"docker", "elasticsearch", "k8s", "kafka", "observability", "ops_journal"} <= set(keys)

    monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", "docker")
    assert "docker" not in [s.key for s in discover_specialists(record=False)]


# ── SpecialistSpec: what a specialist contributes ───────────────────


def _check(*tools: Any) -> HealthCheck:
    return HealthCheck(
        system="Redis", checks="ping and memory.", criteria="critical = down.", tools=tools
    )


def test_a_spec_carries_hint_health_check_and_capabilities():
    agent = _agent("redis_agent", [get_status, restart_service])
    spec = SpecialistSpec(
        agent=agent,
        routing_hint="Redis health, memory, keyspace.",
        health_check=_check(get_status),
        capabilities=frozenset({JOURNAL}),
    )
    [s] = discover_specialists(loader=_eps(("redis", spec), dist="acme-redis-agent"))
    assert s.agent is agent and s.hint == "Redis health, memory, keyspace."
    assert s.health_check is not None and s.health_check.tools == (get_status,)
    assert s.capabilities == frozenset({JOURNAL})
    assert source_of("redis_agent") == "plugin"


def test_a_bare_agent_still_registers_and_routes_on_its_description():
    agent = LlmAgent(name="plain_a", model="gemini-x", description="Plain specialist.")
    [s] = discover_specialists(loader=_eps(("plain", agent)))
    assert s.hint == "Plain specialist." and s.health_check is None and not s.capabilities


def test_a_sweep_may_not_use_a_guarded_tool():
    # An instruction saying "no corrective actions" is not a control: a guarded
    # tool in a sweep would be one model mistake from running unattended.
    spec = SpecialistSpec(
        agent=_agent("redis_agent", [get_status, restart_service]),
        health_check=_check(get_status, restart_service),
    )
    with pytest.raises(ValueError, match="read-only"):
        discover_specialists(loader=_eps(("redis", spec)))


def test_a_sweep_may_only_use_the_specialists_own_tools():
    spec = SpecialistSpec(
        agent=_agent("redis_agent", [get_status]), health_check=_check(get_events)
    )
    with pytest.raises(ValueError, match="not one of redis_agent's own tools"):
        discover_specialists(loader=_eps(("redis", spec)))


def test_a_sweep_needs_tools():
    spec = SpecialistSpec(agent=_agent("redis_agent", [get_status]), health_check=_check())
    with pytest.raises(ValueError, match="no tools"):
        discover_specialists(loader=_eps(("redis", spec)))


def test_the_workspace_specialists_declare_their_contributions():
    by_key = {s.key: s for s in discover_specialists(record=False)}
    for key in ("docker", "elasticsearch", "k8s", "kafka", "observability"):
        assert by_key[key].health_check is not None, key
    assert JOURNAL in by_key["ops_journal"].capabilities
    assert by_key["ops_journal"].health_check is None


# ── Declarative agents ───────────────────────────────────────────────


def _specialists() -> list[Specialist]:
    return [
        Specialist(
            "k8s", _agent("k8s_a", [get_status, get_events, restart_service]), "k8s-health-agent"
        ),
        Specialist("kafka", _agent("kafka_c", [get_status]), "kafka-health-agent"),
    ]


def _file(**agent: Any) -> DeclarativeAgentsFile:
    base = {
        "name": "first_responder",
        "description": "First look at a paging alert",
        "instruction": "Check events first, then status.",
        "tools": ["get_events"],
    }
    return DeclarativeAgentsFile.model_validate({"agents": [{**base, **agent}]})


def test_catalogue_qualifies_names_two_specialists_share():
    catalogue, ambiguous = tool_catalogue(_specialists())

    assert "get_events" in catalogue and "k8s.get_status" in catalogue
    assert "get_status" not in catalogue
    assert ambiguous == {"get_status": ["k8s.get_status", "kafka.get_status"]}


def test_builds_a_read_only_agent_without_a_gate():
    (agent,) = build_declarative_agents(_file(), _specialists(), model="gemini-x")

    assert agent.name == "first_responder"
    assert agent.before_tool_callback is None
    assert source_of("first_responder") == "declarative"


def test_a_guarded_tool_brings_the_confirmation_gate():
    (agent,) = build_declarative_agents(
        _file(tools=["get_events", "restart_service"]), _specialists(), model="gemini-x"
    )

    assert is_confirmation_gate(agent.before_tool_callback)


@pytest.mark.parametrize(
    ("tools", "message"),
    [
        (["get_status"], "several specialists"),
        (["delete_everything"], "unknown tool"),
    ],
)
def test_bad_tool_references_stop_startup(tools, message):
    with pytest.raises(ValueError, match=message):
        build_declarative_agents(_file(tools=tools), _specialists(), model="gemini-x")


def test_a_qualified_reference_resolves_the_ambiguity():
    (agent,) = build_declarative_agents(
        _file(tools=["kafka.get_status"]), _specialists(), model="gemini-x"
    )

    assert agent.tools == [get_status]


def test_names_may_not_collide_with_existing_agents():
    with pytest.raises(ValueError, match="collides"):
        build_declarative_agents(
            _file(name="incident_triage_agent"),
            _specialists(),
            reserved_names=["incident_triage_agent"],
            model="gemini-x",
        )


def test_config_validation():
    with pytest.raises(ValidationError, match="must match"):
        _file(name="Bad Name")
    with pytest.raises(ValidationError, match="duplicate tools"):
        _file(tools=["get_events", "get_events"])


def test_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ORRERY_AGENTS_CONFIG", raising=False)
    assert load_agents_config().agents == []

    path = tmp_path / "agents.yaml"
    path.write_text(
        "agents:\n  - name: first_responder\n    description: First look at an alert\n"
        "    instruction: Check events first.\n    tools: [get_events]\n"
    )
    assert load_agents_config(path).agents[0].name == "first_responder"
