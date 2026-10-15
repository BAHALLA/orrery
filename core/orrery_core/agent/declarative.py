"""Declarative agents: new specialists from existing, reviewed tools — no code.

A team that wants a "capacity planner" or an "on-call first responder" usually
needs the platform's existing tools under a narrower brief, not new tools. A
declarative agent is exactly that: a name, a description the coordinator routes
on, an instruction, and a subset of the **tool catalogue** — every tool of every
installed specialist. It never runs arbitrary code; it can only recombine tools
that already passed review, each still carrying its own guard level.

``ORRERY_AGENTS_CONFIG`` names the file::

    agents:
      - name: capacity_planner
        description: Answers "do we have headroom?" across Kubernetes and Kafka
        instruction: |
          Compare current usage with limits. Lead with the tightest resource.
        tools: [top_nodes, top_pods, get_consumer_lag]
        # A tool name that two specialists share must be qualified:
        #   tools: [k8s.get_events]

Gates apply as everywhere: ``create_agent()`` wires the confirmation gate when
any chosen tool is guarded, RBAC reads the same decorators, and the agent's
instruction ends with the platform's operating principles. An unknown tool, an
ambiguous one, or a name that collides with a specialist stops startup.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_toolset import BaseToolset
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..registry import Specialist, record_source

logger = logging.getLogger("orrery.declarative")

AGENTS_CONFIG_ENV = "ORRERY_AGENTS_CONFIG"
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class DeclarativeAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = Field(min_length=10)
    instruction: str = Field(min_length=10)
    tools: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> DeclarativeAgentConfig:
        if not _NAME.match(self.name):
            raise ValueError(f"name {self.name!r} must match {_NAME.pattern}")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError(f"{self.name}: duplicate tools")
        return self


class DeclarativeAgentsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: list[DeclarativeAgentConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> DeclarativeAgentsFile:
        names = [a.name for a in self.agents]
        if dupes := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate agent names: {sorted(dupes)}")
        return self


def load_agents_config(path: str | os.PathLike[str] | None = None) -> DeclarativeAgentsFile:
    """Read ``ORRERY_AGENTS_CONFIG`` (or *path*). Unset means no agents; a
    configured file that is missing or invalid raises."""
    import yaml

    target = path if path is not None else os.getenv(AGENTS_CONFIG_ENV, "").strip()
    if not target:
        return DeclarativeAgentsFile()
    raw = yaml.safe_load(Path(target).read_text(encoding="utf-8")) or {}
    return DeclarativeAgentsFile.model_validate(raw)


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))


def tool_catalogue(
    specialists: Sequence[Specialist],
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """``(catalogue, ambiguous)`` over every tool of every specialist.

    The catalogue maps ``"key.tool"`` — and the bare ``"tool"`` when only one
    specialist has it — to the tool. ``ambiguous`` maps each bare name several
    specialists share to its qualified forms. Toolsets and delegations are left
    out: a declarative agent recombines concrete, reviewed tools only.
    """
    qualified: dict[str, Any] = {}
    bare: dict[str, list[str]] = {}
    for spec in specialists:
        for tool in getattr(spec.agent, "tools", None) or []:
            if isinstance(tool, (AgentTool, BaseToolset)):
                continue
            name = _tool_name(tool)
            if not name:
                continue
            qualified[f"{spec.key}.{name}"] = tool
            bare.setdefault(name, []).append(f"{spec.key}.{name}")
    catalogue = dict(qualified)
    for name, owners in bare.items():
        if len(owners) == 1:
            catalogue[name] = qualified[owners[0]]
    return catalogue, {n: o for n, o in bare.items() if len(o) > 1}


def build_declarative_agents(
    config: DeclarativeAgentsFile,
    specialists: Sequence[Specialist],
    *,
    reserved_names: Sequence[str] = (),
    model: Any = None,
) -> list[LlmAgent]:
    """Build each configured agent from the catalogue. Raises on any problem."""
    from .base import create_agent
    from .prompts import CONFIRMATION_RULE, OPERATING_PRINCIPLES

    catalogue, ambiguous = tool_catalogue(specialists)
    taken = {s.agent.name for s in specialists} | set(reserved_names)

    agents: list[LlmAgent] = []
    for spec in config.agents:
        if spec.name in taken:
            raise ValueError(f"declarative agent {spec.name!r} collides with an existing agent")
        tools = []
        for ref in spec.tools:
            if ref in ambiguous:
                raise ValueError(
                    f"{spec.name}: tool {ref!r} is offered by several specialists — "
                    f"qualify it as one of {ambiguous[ref]}"
                )
            if ref not in catalogue:
                raise ValueError(f"{spec.name}: unknown tool {ref!r}")
            tools.append(catalogue[ref])
        agent = create_agent(
            name=spec.name,
            description=spec.description,
            instruction=f"{spec.instruction}\n{OPERATING_PRINCIPLES}\n{CONFIRMATION_RULE}",
            tools=tools,
            model=model,
        )
        record_source(spec.name, "declarative")
        taken.add(spec.name)
        agents.append(agent)
    if agents:
        logger.info("Declarative agents enabled: %s", ", ".join(a.name for a in agents))
    return agents
