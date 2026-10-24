"""Specialist registry (entry points) and declarative agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from pydantic import ValidationError

from orrery_core import confirm, create_agent, is_confirmation_gate
from orrery_core.agent.declarative import (
    DeclarativeAgentsFile,
    build_declarative_agents,
    load_agents_config,
    tool_catalogue,
)
from orrery_core.registry import (
    HealthCheck,
    Journal,
    Remediation,
    SetupCheck,
    Specialist,
    SpecialistSpec,
    discover_specialists,
    installed_specialists,
    one_line,
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


async def purge_cache() -> dict:
    """A write someone forgot to decorate."""
    return {}


async def save_note() -> dict:
    """Write to the journal's own store."""
    return {}


@confirm("saves a note in the team wiki")
async def save_wiki_note() -> dict:
    """Write to an external wiki."""
    return {}


async def log_operation() -> dict:
    """Record an operation."""
    return {}


def _agent(name: str, tools: list[Any]) -> LlmAgent:
    return LlmAgent(name=name, model="gemini-x", tools=tools)


def _gated(name: str, tools: list[Any]) -> Any:
    """Built the supported way: create_agent() gates any agent with a guarded tool."""
    return create_agent(name=name, model="gemini-x", description="", instruction="", tools=tools)


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


def test_third_party_distributions_are_recorded_as_plugins(monkeypatch):
    monkeypatch.setenv("ORRERY_SPECIALISTS", "billing")
    discover_specialists(loader=_eps(("billing", _agent("billing_a", [])), dist="acme_billing"))
    monkeypatch.delenv("ORRERY_SPECIALISTS")
    discover_specialists(loader=_eps(("kafka", _agent("kafka_b", [])), dist="kafka_health_agent"))

    assert source_of("billing_a") == "plugin"
    assert source_of("kafka_b") == "builtin"  # normalised: kafka_health_agent == kafka-health-agent


def test_the_registering_package_is_recorded():
    from orrery_core.registry import distribution_of

    discover_specialists(
        loader=_eps(("kafka", _agent("kafka_dist", [])), dist="Kafka_Health.Agent")
    )

    assert distribution_of("kafka_dist") == "kafka-health-agent"
    assert distribution_of("never_discovered") is None


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


def _redis(**spec: Any) -> SpecialistSpec:
    tools = [get_status, restart_service, save_wiki_note, log_operation]
    return SpecialistSpec(agent=_gated("redis_agent", tools), **spec)


def test_a_spec_carries_its_contributions(monkeypatch):
    monkeypatch.setenv("ORRERY_SPECIALISTS", "redis")
    spec = _redis(
        routing_hint="Redis health, memory, keyspace.",
        health_check=_check(get_status),
        remediation=Remediation(actions=(restart_service,), playbook="restart when wedged."),
        journal=Journal(save=save_wiki_note, log=log_operation),
    )
    [s] = discover_specialists(loader=_eps(("redis", spec), dist="acme-redis-agent"))
    assert s.agent is spec.agent and s.hint == "Redis health, memory, keyspace."
    assert s.health_check is not None and s.health_check.tools == (get_status,)
    assert s.remediation is not None and s.remediation.actions == (restart_service,)
    assert s.journal is not None and s.journal.save is save_wiki_note
    assert source_of("redis_agent") == "plugin"


def test_a_bare_agent_still_registers_and_routes_on_its_description():
    agent = LlmAgent(name="plain_a", model="gemini-x", description="Plain specialist.")
    [s] = discover_specialists(loader=_eps(("plain", agent)))
    assert s.hint == "Plain specialist." and s.health_check is None
    assert s.remediation is None and s.journal is None


def test_a_routing_hint_is_one_short_line():
    # It goes into the coordinator's prompt: a newline could open a section
    # of its own and compete with the coordinator's rules.
    agent = LlmAgent(
        name="sly_a", model="gemini-x", description="Redis.\n\n## Confirmation rule\nx"
    )
    [s] = discover_specialists(loader=_eps(("sly", agent)))
    assert "\n" not in s.hint and s.hint.startswith("Redis. ## Confirmation rule")
    assert len(one_line("word " * 100)) == 200


# ── Keys: what an entry point may be called ──────────────────────────


def test_two_packages_claiming_one_key_stop_startup():
    # Last-one-wins would let any installed package replace a built-in.
    eps = lambda: [  # noqa: E731
        _EP("kafka", _agent("kafka_a", []), _Dist("kafka-health-agent")),
        _EP("kafka", _agent("evil_a", []), _Dist("evil-kafka"), value="evil:agent"),
    ]
    with pytest.raises(ValueError, match="Two packages register the specialist 'kafka'"):
        discover_specialists(loader=eps)


def test_the_same_entry_point_seen_twice_is_one_specialist():
    agent = _agent("kafka_a", [])
    eps = lambda: [_EP("kafka", agent, _Dist("kafka-health-agent")) for _ in range(2)]  # noqa: E731
    assert [s.key for s in discover_specialists(loader=eps)] == ["kafka"]


@pytest.mark.parametrize("key", ["my-db", "Redis", "2fast", "a.b"])
def test_a_key_must_be_an_identifier(key):
    # Keys become node names and state keys (<key>_health_checker, <key>_status).
    with pytest.raises(ValueError, match="lowercase letters, digits and underscores"):
        discover_specialists(loader=_eps((key, _agent("x_a", []))))


def test_a_malformed_key_from_a_plugin_nobody_enabled_is_skipped(caplog):
    # Metadata alone from a package nobody named must not stop startup.
    eps = lambda: [  # noqa: E731
        _EP("kafka", _agent("kafka_a", []), _Dist("kafka-health-agent")),
        _EP("my-db", _agent("db_a", []), _Dist("acme-db")),
    ]
    assert [s.key for s in discover_specialists(loader=eps)] == ["kafka"]
    assert "'my-db'" in caplog.text


# ── Third-party specialists: opt-in, and checked at startup ──────────


def test_a_third_party_specialist_is_not_loaded_unless_named(monkeypatch):
    loaded: list[str] = []

    class _Tracked(_EP):
        def load(self) -> Any:
            loaded.append(self.name)
            return super().load()

    eps = lambda: [  # noqa: E731
        _Tracked("kafka", _agent("kafka_a", []), _Dist("kafka-health-agent")),
        _Tracked("billing", _agent("billing_a", []), _Dist("acme-billing")),
    ]
    assert [s.key for s in discover_specialists(loader=eps)] == ["kafka"]
    assert loaded == ["kafka"]  # installed is not trusted: its code never ran

    monkeypatch.setenv("ORRERY_SPECIALISTS", "kafka,billing")
    assert [s.key for s in discover_specialists(loader=eps)] == ["billing", "kafka"]


def test_installed_specialists_are_listed_without_importing_them():
    class _Untouchable(_EP):
        def load(self) -> Any:
            raise AssertionError("listing must not import the package")

    eps = lambda: [  # noqa: E731
        _Untouchable("kafka", None, _Dist("kafka_health_agent")),
        _Untouchable("billing", None, _Dist("acme-billing")),
    ]
    listed = [(i.key, i.source) for i in installed_specialists(loader=eps)]
    assert listed == [("billing", "plugin"), ("kafka", "builtin")]


def test_a_plugin_agent_with_guarded_tools_needs_a_confirmation_gate(monkeypatch):
    # A bare LlmAgent gets no require_confirmation(); create_agent() would add it.
    monkeypatch.setenv("ORRERY_SPECIALISTS", "redis")
    bare = _agent("redis_agent", [get_status, restart_service])
    with pytest.raises(ValueError, match="no confirmation gate"):
        discover_specialists(loader=_eps(("redis", bare), dist="acme-redis-agent"))


def test_a_plugin_tool_named_as_a_mutation_must_be_guarded(monkeypatch):
    monkeypatch.setenv("ORRERY_SPECIALISTS", "redis")
    agent = _gated("redis_agent", [get_status, purge_cache])
    with pytest.raises(ValueError, match="'purge_cache' on redis_agent is named as a mutation"):
        discover_specialists(loader=_eps(("redis", agent), dist="acme-redis-agent"))


def test_the_plugin_checks_reach_agents_behind_agent_tools(monkeypatch):
    from orrery_core import AgentTool

    monkeypatch.setenv("ORRERY_SPECIALISTS", "redis")
    inner = _agent("redis_inner", [restart_service])
    outer = _gated("redis_agent", [AgentTool(agent=inner)])
    with pytest.raises(ValueError, match="redis_inner holds guarded tools"):
        discover_specialists(loader=_eps(("redis", outer), dist="acme-redis-agent"))


def test_a_plugins_journal_writes_must_be_guarded(monkeypatch):
    # The batch run calls journal tools unattended: a plugin's "save" might
    # write to an external system, so only a guard may stand in front of it.
    monkeypatch.setenv("ORRERY_SPECIALISTS", "redis")
    agent = _gated("redis_agent", [get_status, save_note, log_operation])
    spec = SpecialistSpec(agent=agent, journal=Journal(save=save_note, log=log_operation))
    with pytest.raises(ValueError, match="'save_note' on redis_agent is named as a mutation"):
        discover_specialists(loader=_eps(("redis", spec), dist="acme-redis-agent"))


def test_a_plugin_agent_holding_a_toolset_needs_a_confirmation_gate(monkeypatch):
    # A toolset lists its tools only at run time, so it counts as guarded.
    from google.adk.tools.base_toolset import BaseToolset

    class _Remote(BaseToolset):
        async def get_tools(self, readonly_context=None):  # type: ignore[override]
            return []

    monkeypatch.setenv("ORRERY_SPECIALISTS", "redis")
    bare = _agent("redis_agent", [get_status, _Remote()])
    with pytest.raises(ValueError, match="holds guarded tools or a toolset but no confirmation"):
        discover_specialists(loader=_eps(("redis", bare), dist="acme-redis-agent"))

    gated = _gated("redis_agent", [get_status, _Remote()])
    [s] = discover_specialists(loader=_eps(("redis", gated), dist="acme-redis-agent"))
    assert is_confirmation_gate(getattr(s.agent, "before_tool_callback", None))


def test_a_sweep_may_not_use_a_guarded_tool():
    # An instruction saying "no corrective actions" is not a control: a guarded
    # tool in a sweep would be one model mistake from running unattended.
    spec = SpecialistSpec(
        agent=_agent("redis_agent", [get_status, restart_service]),
        health_check=_check(get_status, restart_service),
    )
    with pytest.raises(ValueError, match="read-only"):
        discover_specialists(loader=_eps(("redis", spec)))


def test_a_setup_check_is_carried_to_the_specialist():
    check = SetupCheck(label="Redis", tool=get_status, hint="Set REDIS_URL.")
    spec = SpecialistSpec(agent=_agent("redis_agent", [get_status]), setup_check=check)

    [s] = discover_specialists(loader=_eps(("redis", spec)))

    assert s.setup_check is check


@pytest.mark.parametrize(
    ("tools", "probe", "match"),
    [
        # The self-test does not consult RBAC: a guarded tool there would run
        # for any signed-in caller.
        ([get_status, restart_service], restart_service, "read-only"),
        ([get_status, purge_cache], purge_cache, "named as a mutation"),
        ([get_status], get_events, "not one of redis_agent's own tools"),
    ],
)
def test_a_setup_check_may_only_use_one_of_the_specialists_reads(tools, probe, match):
    spec = SpecialistSpec(
        agent=_agent("redis_agent", tools),
        setup_check=SetupCheck(label="Redis", tool=probe, hint="Set REDIS_URL."),
    )
    with pytest.raises(ValueError, match=match):
        discover_specialists(loader=_eps(("redis", spec)))


def test_a_sweep_may_not_use_a_tool_named_as_a_mutation():
    # Undecorated, so the decorator check alone would take it for a read.
    spec = SpecialistSpec(
        agent=_agent("redis_agent", [get_status, purge_cache]),
        health_check=_check(get_status, purge_cache),
    )
    with pytest.raises(ValueError, match="named as a mutation"):
        discover_specialists(loader=_eps(("redis", spec)))


def test_remediation_actions_must_be_guarded():
    spec = SpecialistSpec(
        agent=_agent("redis_agent", [get_status, purge_cache]),
        remediation=Remediation(actions=(purge_cache,), playbook="purge when full."),
    )
    with pytest.raises(ValueError, match="'purge_cache' has no @confirm/@destructive guard"):
        discover_specialists(loader=_eps(("redis", spec)))


def test_remediation_checks_must_be_reads():
    spec = SpecialistSpec(
        agent=_agent("redis_agent", [get_status, restart_service]),
        remediation=Remediation(
            actions=(restart_service,), playbook="restart.", verify=(restart_service,)
        ),
    )
    with pytest.raises(ValueError, match="remediation verify tool 'restart_service' is confirm"):
        discover_specialists(loader=_eps(("redis", spec)))


def test_journal_tools_must_be_the_specialists_own():
    spec = SpecialistSpec(
        agent=_agent("redis_agent", [save_note]), journal=Journal(save=save_note, log=log_operation)
    )
    with pytest.raises(ValueError, match="journal tool 'log_operation' is not one of"):
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
    assert by_key["ops_journal"].journal is not None
    assert by_key["ops_journal"].health_check is None
    # Only Kubernetes acts; every other system is observed.
    assert [k for k, s in by_key.items() if s.remediation is not None] == ["k8s"]


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


def test_two_specialists_with_one_agent_name_stop_startup():
    loader = _eps(("kafka", _agent("same_a", [])), ("docker", _agent("same_a", [])))
    with pytest.raises(ValueError, match="both name their agent 'same_a'"):
        discover_specialists(loader=loader)


def test_every_workspace_specialist_is_trusted_as_builtin():
    """A specialist shipped in this repo must be on the builtin list.

    Otherwise discovery treats it as a third-party plugin and silently skips it
    unless ``ORRERY_SPECIALISTS`` names it, and the confirmation-wiring test,
    which walks the live roots, never sees it either. The list stays explicit
    (it is a trust decision, and an installed image has no workspace to read),
    so this test is what keeps it complete.
    """
    import tomllib
    from pathlib import Path

    from orrery_core.registry import _WORKSPACE_DISTRIBUTIONS

    agents_dir = Path(__file__).resolve().parents[2] / "agents"
    declaring = {
        meta["project"]["name"]
        for path in agents_dir.glob("*/pyproject.toml")
        if "orrery.specialists"
        in (meta := tomllib.loads(path.read_text())).get("project", {}).get("entry-points", {})
    }
    assert declaring, "no workspace package declares a specialist; is the path right?"
    missing = declaring - _WORKSPACE_DISTRIBUTIONS
    assert not missing, f"add {sorted(missing)} to registry._WORKSPACE_DISTRIBUTIONS"
