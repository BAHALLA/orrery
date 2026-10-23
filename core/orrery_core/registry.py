"""Specialist registry: which agents the coordinator can delegate to.

The chat root used to import each specialist by name, so adding one meant
editing ``orrery_assistant/agent.py`` and cutting a release. Specialists now
**register themselves** with a Python entry point, and the coordinator asks the
registry instead::

    # agents/kafka-health/pyproject.toml
    [project.entry-points."orrery.specialists"]
    kafka = "kafka_health_agent.agent:root_agent"

Any installed package can do the same — a team's own specialist is a
``pip install`` away, with no fork of this repository.

An entry point resolves to an agent, or to a :class:`SpecialistSpec` when the
specialist has more to say than its ``description``: a one-line routing hint,
its part of a health sweep (:class:`HealthCheck`), the actions it offers the
unattended remediation loop (:class:`Remediation`), a place to keep records
(:class:`Journal`) and its row in the console's first-run self-test
(:class:`SetupCheck`). The coordinator knows nothing about any specialist by name;
everything it says about one comes from here, so a third-party specialist
routes, sweeps, remediates and journals exactly like a built-in one.

Operators choose what is live per deployment:

- ``ORRERY_SPECIALISTS=kafka,k8s`` — only these (default: every built-in one);
- ``ORRERY_DISABLED_SPECIALISTS=docker`` — all but these.

A **third-party** specialist (one not shipped in this repository) is never
loaded by default: installing a package, or a dependency pulling one in, must
not be enough to put code in the coordinator's prompt and tool list. It is
loaded only when ``ORRERY_SPECIALISTS`` names it, and it is checked harder than
a built-in one, at startup (see :func:`_check_plugin_gates`).

An unknown name in either list, two packages claiming one key, or a key that is
not a plain identifier stops startup: a typo that silently enabled everything
(or nothing), or a package quietly replacing a built-in, is the kind of mistake
nobody notices until an incident.

Two other sources add specialists at run time and are recorded here too, so the
admin view can say where each agent came from: declarative agents
(:mod:`orrery_core.agent.declarative`) and MCP servers
(:mod:`orrery_core.tools.mcp`).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Literal

from google.adk.agents.base_agent import BaseAgent

logger = logging.getLogger("orrery.registry")

#: Entry-point group a specialist package registers under.
ENTRY_POINT_GROUP = "orrery.specialists"

ENABLED_ENV = "ORRERY_SPECIALISTS"
DISABLED_ENV = "ORRERY_DISABLED_SPECIALISTS"

Source = Literal["builtin", "plugin", "declarative", "mcp"]

# agent name -> where it came from; read by the admin inventory.
_SOURCES: dict[str, Source] = {}


def record_source(agent_name: str, source: Source) -> None:
    """Remember where an agent came from, for the admin inventory."""
    _SOURCES[agent_name] = source


def source_of(agent_name: str) -> Source | None:
    return _SOURCES.get(agent_name)


@dataclass(frozen=True)
class HealthCheck:
    """A specialist's part of a health sweep ("is everything healthy?").

    The sweeps run unattended, so this is a contract, not free text: ``tools``
    are called once each and must be **read-only** tools of the specialist's
    own agent (checked at discovery), ``checks`` says what they cover, and
    ``criteria`` is the severity rubric (critical / degraded / healthy) the
    verdict is held to. One declaration feeds both the conversational sweep
    and the batch workflow's checker, so the two cannot drift apart.
    """

    #: Human name of the system, e.g. ``"Kafka"``.
    system: str
    checks: str
    criteria: str
    tools: tuple[Any, ...]


@dataclass(frozen=True)
class Remediation:
    """What a specialist lets the unattended remediation loop do.

    ``actions`` must be **guarded** (``@confirm`` / ``@destructive``) tools of
    the specialist's own agent, so every one of them stops at the confirmation
    gate however the loop's model behaves. ``playbook`` tells the actor which
    action fits which diagnosis, smallest blast radius first. ``verify`` are
    read-only tools that show whether an action worked (the system's
    health-check tools are offered to the verifier as well).
    """

    actions: tuple[Any, ...]
    playbook: str
    verify: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Journal:
    """The specialist keeps records: the coordinator may offer to save findings.

    ``save`` stores a note (the batch run saves its triage report with it) and
    ``log`` records an operation. The batch run calls both **unattended**. The
    built-in ops journal writes only to its own session/user state, so its
    tools need no confirmation; a third-party journal gets no such exemption:
    its tools named as writes must be guarded, so an unattended save into an
    external system stops at the confirmation gate.
    """

    save: Any
    log: Any


@dataclass(frozen=True)
class SetupCheck:
    """The console's first-run self-test for this specialist: is it wired at all?

    Not "is the system healthy" (the agent answers that) but "can Orrery reach
    it", the failure a new user hits first. ``tool`` is the specialist's single
    cheapest **read-only** tool, called with no arguments; the self-test runs
    on an authenticated path that does not consult RBAC, so the tool is
    checked at discovery like a health check's. ``hint`` is what to configure
    when it fails.
    """

    #: Human name shown in the console, e.g. ``"Kafka"``.
    label: str
    tool: Any
    hint: str


@dataclass(frozen=True)
class SpecialistSpec:
    """What a specialist package registers: its agent and what it contributes."""

    agent: BaseAgent
    #: One line the coordinator routes on. Defaults to the agent's description.
    routing_hint: str = ""
    health_check: HealthCheck | None = None
    remediation: Remediation | None = None
    journal: Journal | None = None
    setup_check: SetupCheck | None = None


#: Longest routing line taken from a specialist: it goes into the
#: coordinator's own prompt, so it is one line and short.
MAX_HINT_CHARS = 200


def one_line(text: str, limit: int = MAX_HINT_CHARS) -> str:
    """*text* as a single line of at most *limit* characters.

    Specialist text reaches the coordinator's instruction verbatim; a newline
    there could open a section of its own ("## Confirmation rule") and compete
    with the coordinator's real rules.
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class Specialist:
    """One registered specialist: its entry-point key, agent and contributions."""

    key: str
    agent: BaseAgent
    distribution: str
    routing_hint: str = ""
    health_check: HealthCheck | None = None
    remediation: Remediation | None = None
    journal: Journal | None = None
    setup_check: SetupCheck | None = None

    @property
    def hint(self) -> str:
        """What the coordinator says this specialist is for, as one short line."""
        return one_line(self.routing_hint or self.agent.description or self.agent.name)


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", "") or tool)


def _own_tools(key: str, agent: BaseAgent, tools: tuple[Any, ...], what: str) -> None:
    own = {id(t) for t in getattr(agent, "tools", None) or []}
    for tool in tools:
        if id(tool) not in own:
            raise ValueError(
                f"specialist {key!r}: {what} tool {_tool_name(tool)!r} is not one of "
                f"{agent.name}'s own tools"
            )


def _read_only(key: str, tools: tuple[Any, ...], what: str) -> None:
    """Each tool is a read: no guard decorator, and not *named* as a write.

    The name check catches the mutating tool someone forgot to decorate, which
    the decorator check alone would wave through as a read.
    """
    from .security.classify import looks_mutating
    from .security.guardrails import get_guard_level

    for tool in tools:
        name = _tool_name(tool)
        if (level := get_guard_level(tool)) is not None:
            raise ValueError(
                f"specialist {key!r}: {what} tool {name!r} is {level}; "
                "it may only use read-only tools"
            )
        if looks_mutating(name):
            raise ValueError(
                f"specialist {key!r}: {what} tool {name!r} is named as a mutation; "
                "it may only use read-only tools"
            )


def _validate_health_check(key: str, agent: BaseAgent, check: HealthCheck) -> None:
    """A sweep may only call read-only tools its own specialist exposes.

    The sweep runs with "no corrective actions" in its instruction, but an
    instruction is not a control: a guarded tool in the list would be one model
    mistake from running unattended. Refused here, at startup, instead.
    """
    if not check.tools:
        raise ValueError(f"specialist {key!r}: health_check has no tools")
    _own_tools(key, agent, check.tools, "health_check")
    _read_only(key, check.tools, "health_check")


def _validate_remediation(key: str, agent: BaseAgent, remediation: Remediation) -> None:
    """Every action is guarded, so the loop's gate stops each one; checks are reads."""
    from .security.guardrails import get_guard_level

    if not remediation.actions:
        raise ValueError(f"specialist {key!r}: remediation has no actions")
    _own_tools(key, agent, remediation.actions, "remediation")
    for tool in remediation.actions:
        if get_guard_level(tool) is None:
            raise ValueError(
                f"specialist {key!r}: remediation action {_tool_name(tool)!r} has no "
                "@confirm/@destructive guard; an unattended loop may only take guarded actions"
            )
    _own_tools(key, agent, remediation.verify, "remediation verify")
    _read_only(key, remediation.verify, "remediation verify")


def _validate_journal(key: str, agent: BaseAgent, journal: Journal) -> None:
    _own_tools(key, agent, (journal.save, journal.log), "journal")


def _validate_setup_check(key: str, agent: BaseAgent, check: SetupCheck) -> None:
    """The self-test skips RBAC, so its tool must be one of the specialist's reads."""
    _own_tools(key, agent, (check.tool,), "setup_check")
    _read_only(key, (check.tool,), "setup_check")


def _agents_in(agent: BaseAgent) -> list[BaseAgent]:
    """*agent* and every agent it can reach: sub-agents and agents behind AgentTools."""
    seen: list[BaseAgent] = []
    stack = [agent]
    while stack:
        current = stack.pop()
        if any(current is s for s in seen):
            continue
        seen.append(current)
        stack.extend(getattr(current, "sub_agents", None) or [])
        stack.extend(
            t.agent
            for t in getattr(current, "tools", None) or []
            if isinstance(getattr(t, "agent", None), BaseAgent)
        )
    return seen


def _check_plugin_gates(key: str, spec: SpecialistSpec) -> None:
    """Hold a third-party specialist to the invariants the repo's tests hold ours to.

    Built-in specialists are checked by ``tests/test_confirmation_wiring.py``
    at build time; a plugin's code never meets those tests, so the same two
    rules run here, at startup, for every agent the plugin can reach:

    - a tool *named* as a mutation must carry ``@confirm``/``@destructive``
      (an undecorated one is treated as a read everywhere: no gate, no L2
      block, callable by a viewer). This includes its :class:`Journal` tools:
      the batch run calls those unattended, and only a guard stops a journal
      whose "save" writes somewhere other than its own store;
    - an agent holding a guarded tool **or a toolset** must have a
      confirmation gate (``create_agent()`` adds one; a bare ``LlmAgent`` does
      not). A toolset (MCP, OpenAPI) lists its tools only at run time, so it
      cannot be shown safe here and counts as guarded, as it does in
      ``create_agent()``.
    """
    from google.adk.tools.base_toolset import BaseToolset

    from .security.classify import looks_mutating
    from .security.guardrails import is_confirmation_gate, is_guarded

    for agent in _agents_in(spec.agent):
        tools = [
            t
            for t in getattr(agent, "tools", None) or []
            if not isinstance(getattr(t, "agent", None), BaseAgent)
        ]
        for tool in tools:
            if isinstance(tool, BaseToolset):
                continue  # no static names to check; gated below
            name = _tool_name(tool)
            if looks_mutating(name) and not is_guarded(tool):
                raise ValueError(
                    f"third-party specialist {key!r}: tool {name!r} on {agent.name} is named "
                    "as a mutation but has no @confirm/@destructive guard"
                )
        gated = [t for t in tools if isinstance(t, BaseToolset) or is_guarded(t)]
        if gated and not is_confirmation_gate(getattr(agent, "before_tool_callback", None)):
            raise ValueError(
                f"third-party specialist {key!r}: {agent.name} holds guarded tools or a "
                "toolset but no "
                "confirmation gate; build it with orrery_core.create_agent() or add "
                "require_confirmation() to its before_tool_callback"
            )


def _as_spec(key: str, ep: Any, obj: Any, *, plugin: bool) -> SpecialistSpec:
    if isinstance(obj, SpecialistSpec):
        spec = obj
    elif isinstance(obj, BaseAgent):
        spec = SpecialistSpec(agent=obj)
    else:
        raise ValueError(
            f"Entry point {ENTRY_POINT_GROUP}:{key} ({ep.value}) is a "
            f"{type(obj).__name__}, not an agent or a SpecialistSpec"
        )
    if not isinstance(spec.agent, BaseAgent):
        raise ValueError(
            f"Entry point {ENTRY_POINT_GROUP}:{key}: SpecialistSpec.agent is not an agent"
        )
    if spec.health_check is not None:
        _validate_health_check(key, spec.agent, spec.health_check)
    if spec.remediation is not None:
        _validate_remediation(key, spec.agent, spec.remediation)
    if spec.journal is not None:
        _validate_journal(key, spec.agent, spec.journal)
    if spec.setup_check is not None:
        _validate_setup_check(key, spec.agent, spec.setup_check)
    if plugin:
        _check_plugin_gates(key, spec)
    return spec


def _names(env: str) -> set[str]:
    return {n.strip() for n in os.getenv(env, "").split(",") if n.strip()}


#: A key names the specialist in env lists, in the admin catalog, and in the
#: batch workflow's node names and state keys (``<key>_health_checker``,
#: ``<key>_status``), which must be Python identifiers.
_KEY = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class Installed:
    """An installed specialist, known from package metadata alone (nothing imported)."""

    key: str
    distribution: str
    source: Source


def _installed(loader: Callable[[], Any] | None) -> dict[str, tuple[Installed, Any]]:
    eps = list((loader or (lambda: entry_points(group=ENTRY_POINT_GROUP)))())
    found: dict[str, tuple[Installed, Any]] = {}
    for ep in eps:
        # Normalised (PEP 503): installers record `docker_agent` or `docker-agent`.
        dist = re.sub(r"[-_.]+", "-", getattr(getattr(ep, "dist", None), "name", "") or "").lower()
        entry = Installed(
            ep.name, dist, "builtin" if dist in _WORKSPACE_DISTRIBUTIONS else "plugin"
        )
        if not _KEY.fullmatch(ep.name):
            message = (
                f"Entry point {ENTRY_POINT_GROUP}:{ep.name!r} ({ep.value}): a specialist "
                "key must be lowercase letters, digits and underscores, starting with a letter"
            )
            if entry.source == "builtin":
                raise ValueError(message)
            # A package nobody enabled must not be able to stop startup with
            # its metadata alone; it could never be named anyway.
            logger.warning("%s; ignored", message)
            continue
        if ep.name in found:
            other, other_ep = found[ep.name]
            if (other.distribution, other_ep.value) == (dist, ep.value):
                continue  # the same entry point seen twice (e.g. an editable install)
            # Last-one-wins would let any package silently replace a built-in.
            raise ValueError(
                f"Two packages register the specialist {ep.name!r}: "
                f"{other.distribution or '?'} ({other_ep.value}) and {dist or '?'} ({ep.value})"
            )
        found[ep.name] = (entry, ep)
    return found


def installed_specialists(*, loader: Callable[[], Any] | None = None) -> list[Installed]:
    """Every installed specialist, sorted by key, read from metadata without importing it.

    For the admin view: listing what is installed must not run the code of a
    specialist the deployment excludes.
    """
    return [entry for entry, _ in sorted(_installed(loader).values(), key=lambda e: e[0].key)]


def discover_specialists(
    *, loader: Callable[[], Any] | None = None, record: bool = True
) -> list[Specialist]:
    """The specialists this deployment runs, sorted by key, loaded and validated.

    Built-in specialists are on unless the env lists say otherwise; a
    third-party one only when ``ORRERY_SPECIALISTS`` names it. ``loader``
    exists for tests; it returns the entry points to consider.

    Raises:
        ValueError: An env list names a specialist that is not installed, two
            packages register one key, a key is not an identifier, an entry
            point does not resolve to an agent, or a spec breaks a rule.
    """
    installed = _installed(loader)
    enabled, disabled = _names(ENABLED_ENV), _names(DISABLED_ENV)
    if unknown := (enabled | disabled) - set(installed):
        raise ValueError(
            f"{ENABLED_ENV}/{DISABLED_ENV} name specialists that are not installed: "
            f"{sorted(unknown)}. Installed: {sorted(installed)}"
        )

    selected: list[Specialist] = []
    for key in sorted(installed):
        entry, ep = installed[key]
        if (enabled and key not in enabled) or key in disabled:
            logger.info("Specialist %s disabled by configuration", key)
            continue
        if entry.source == "plugin" and key not in enabled:
            logger.warning(
                "Third-party specialist %s (%s) is installed but not loaded: name it in %s "
                "to trust it",
                key,
                entry.distribution or "unknown package",
                ENABLED_ENV,
            )
            continue
        spec = _as_spec(key, ep, ep.load(), plugin=entry.source == "plugin")
        if clash := next((s for s in selected if s.agent.name == spec.agent.name), None):
            # The coordinator calls each as an AgentTool named after the agent:
            # two with one name would be one tool shadowing the other.
            raise ValueError(
                f"Specialists {clash.key!r} and {key!r} both name their agent {spec.agent.name!r}"
            )
        selected.append(
            Specialist(
                key=key,
                agent=spec.agent,
                distribution=entry.distribution,
                routing_hint=spec.routing_hint,
                health_check=spec.health_check,
                remediation=spec.remediation,
                journal=spec.journal,
                setup_check=spec.setup_check,
            )
        )
        if record:
            record_source(spec.agent.name, entry.source)
    return selected


# Distributions that ship in this repository. Anything else registering a
# specialist is a third-party plugin, which the admin view calls out. Kept
# explicit (it is a trust decision); test_registry checks it lists every
# workspace package with an ``orrery.specialists`` entry point.
_WORKSPACE_DISTRIBUTIONS = frozenset(
    {
        "docker-agent",
        "elasticsearch-agent",
        "kafka-health-agent",
        "k8s-health-agent",
        "observability-agent",
        "ops-journal",
        "orrery-assistant",
    }
)
