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
its part of a health sweep (:class:`HealthCheck`), and capabilities the
coordinator may offer (:data:`JOURNAL`). The coordinator knows nothing about
any specialist by name; everything it says about one comes from here, so a
third-party specialist routes, sweeps and offers exactly like a built-in one.

Operators choose what is live per deployment:

- ``ORRERY_SPECIALISTS=kafka,k8s`` — only these (default: every installed one);
- ``ORRERY_DISABLED_SPECIALISTS=docker`` — all but these.

An unknown name in either list stops startup: a typo that silently enabled
everything (or nothing) is the kind of mistake nobody notices until an
incident.

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


#: Capability: the specialist can save findings for later (notes, journal).
#: The coordinator offers "want me to save this?" only when one is enabled.
JOURNAL = "journal"


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
class SpecialistSpec:
    """What a specialist package registers: its agent and what it contributes."""

    agent: BaseAgent
    #: One line the coordinator routes on. Defaults to the agent's description.
    routing_hint: str = ""
    health_check: HealthCheck | None = None
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Specialist:
    """One registered specialist: its entry-point key, agent and contributions."""

    key: str
    agent: BaseAgent
    distribution: str
    routing_hint: str = ""
    health_check: HealthCheck | None = None
    capabilities: frozenset[str] = frozenset()

    @property
    def hint(self) -> str:
        """What the coordinator says this specialist is for."""
        return self.routing_hint or self.agent.description or self.agent.name


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", "") or tool)


def _validate_health_check(key: str, agent: BaseAgent, check: HealthCheck) -> None:
    """A sweep may only call read-only tools its own specialist exposes.

    The sweep runs with "no corrective actions" in its instruction, but an
    instruction is not a control: a guarded tool in the list would be one model
    mistake from running unattended. Refused here, at startup, instead.
    """
    from .security.guardrails import get_guard_level

    if not check.tools:
        raise ValueError(f"specialist {key!r}: health_check has no tools")
    own = {id(t) for t in getattr(agent, "tools", None) or []}
    for tool in check.tools:
        if id(tool) not in own:
            raise ValueError(
                f"specialist {key!r}: health_check tool {_tool_name(tool)!r} is not one of "
                f"{agent.name}'s own tools"
            )
        if (level := get_guard_level(tool)) is not None:
            raise ValueError(
                f"specialist {key!r}: health_check tool {_tool_name(tool)!r} is "
                f"{level}; a sweep may only use read-only tools"
            )


def _as_spec(key: str, ep: Any, obj: Any) -> SpecialistSpec:
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
    return spec


def _names(env: str) -> set[str]:
    return {n.strip() for n in os.getenv(env, "").split(",") if n.strip()}


def discover_specialists(
    *, loader: Callable[[], Any] | None = None, record: bool = True, apply_env: bool = True
) -> list[Specialist]:
    """Every installed specialist, sorted by key, filtered by the env lists.

    ``apply_env=False`` skips the filtering — for the admin view, which shows
    env-excluded specialists as unavailable rather than hiding them. ``loader``
    exists for tests; it returns the entry points to load.

    Raises:
        ValueError: An env list names a specialist that is not installed, or an
            entry point does not resolve to an agent.
    """
    eps = list((loader or (lambda: entry_points(group=ENTRY_POINT_GROUP)))())
    by_key = {ep.name: ep for ep in eps}

    enabled, disabled = (_names(ENABLED_ENV), _names(DISABLED_ENV)) if apply_env else (set(), set())
    if unknown := (enabled | disabled) - set(by_key):
        raise ValueError(
            f"{ENABLED_ENV}/{DISABLED_ENV} name specialists that are not installed: "
            f"{sorted(unknown)}. Installed: {sorted(by_key)}"
        )

    selected: list[Specialist] = []
    for key in sorted(by_key):
        if (enabled and key not in enabled) or key in disabled:
            logger.info("Specialist %s disabled by configuration", key)
            continue
        ep = by_key[key]
        spec = _as_spec(key, ep, ep.load())
        agent = spec.agent
        # Normalised (PEP 503): installers record `docker_agent` or `docker-agent`.
        dist = re.sub(r"[-_.]+", "-", getattr(getattr(ep, "dist", None), "name", "") or "").lower()
        selected.append(
            Specialist(
                key=key,
                agent=agent,
                distribution=dist,
                routing_hint=spec.routing_hint,
                health_check=spec.health_check,
                capabilities=frozenset(spec.capabilities),
            )
        )
        if record:
            # Shipped in this repository's workspace vs. installed from elsewhere.
            record_source(agent.name, "builtin" if dist in _WORKSPACE_DISTRIBUTIONS else "plugin")
    return selected


# Distributions that ship in this repository. Anything else registering a
# specialist is a third-party plugin, which the admin view calls out.
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
