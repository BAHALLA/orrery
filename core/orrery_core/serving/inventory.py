"""What the coordinator can actually reach: specialists, their tools, guard levels.

Read from the live agent tree rather than from configuration, so the admin view
cannot disagree with what is running — a specialist disabled by
``ORRERY_DISABLED_SPECIALISTS`` is absent here because it is absent there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_toolset import BaseToolset

from ..registry import distribution_of, key_of, source_of
from ..security.guardrails import get_guard_level, get_guard_reason, is_confirmation_gate

Guard = Literal["read", "confirm", "destructive", "dynamic"]


@dataclass(frozen=True)
class ToolEntry:
    name: str
    #: ``dynamic`` for a toolset (MCP): its tools, and their levels, exist only
    #: at run time — gated as a whole, classified per tool when discovered.
    guard: Guard
    reason: str = ""


@dataclass(frozen=True)
class AgentEntry:
    name: str
    description: str
    source: str
    tools: list[ToolEntry] = field(default_factory=list)
    #: The entry-point key (``kafka``); empty for declarative and MCP agents.
    key: str = ""
    #: The installed package that registered it, and that package's version.
    package: str = ""
    version: str = ""
    #: The model the agent calls (``a → b`` for a fallback chain).
    model: str = ""
    #: Whether a marked confirmation gate guards its tools.
    gated: bool = False


@cache
def package_version(distribution: str) -> str:
    """The installed version of *distribution*, or ``""`` when it isn't installed."""
    try:
        return version(distribution)
    except PackageNotFoundError:
        return ""


def model_label(model: Any) -> str:
    """A model as an admin reads it: its name, or each name of a fallback chain."""
    if model is None or model == "":
        return ""
    if isinstance(model, str):
        return model
    targets = getattr(model, "targets", None)
    if isinstance(targets, list) and targets:
        return " → ".join(model_label(t) for t in targets)
    return str(getattr(model, "model", "") or type(model).__name__)


def _tool_entry(tool: Any) -> ToolEntry:
    if isinstance(tool, BaseToolset):
        prefix = getattr(tool, "tool_name_prefix", None) or type(tool).__name__
        return ToolEntry(name=f"{prefix}_*", guard="dynamic", reason="tools discovered at run time")
    name = str(getattr(tool, "name", None) or getattr(tool, "__name__", type(tool).__name__))
    level = get_guard_level(tool)
    guard: Guard = (
        "read" if level is None else ("destructive" if level == "destructive" else "confirm")
    )
    return ToolEntry(name=name, guard=guard, reason=get_guard_reason(tool) if level else "")


def specialist_entries(root: Any) -> list[AgentEntry]:
    """The entries that are specialists: registered by an entry point, a
    declarative file or an MCP config. The coordinator's own helpers (the
    triage sweep agent) are AgentTools too, but not systems a user asks about.
    """
    return [entry for entry in agent_inventory(root) if source_of(entry.name)]


def agent_inventory(root: Any) -> list[AgentEntry]:
    """One entry per agent the root can delegate to. Empty for a graph root."""
    entries: list[AgentEntry] = []
    for tool in getattr(root, "tools", None) or []:
        if not isinstance(tool, AgentTool):
            continue
        agent = tool.agent
        package = distribution_of(agent.name) or ""
        entries.append(
            AgentEntry(
                name=agent.name,
                description=agent.description or "",
                source=source_of(agent.name) or "builtin",
                tools=[
                    _tool_entry(t)
                    for t in getattr(agent, "tools", None) or []
                    if not isinstance(t, AgentTool)
                ],
                key=key_of(agent.name) or "",
                package=package,
                version=package_version(package) if package else "",
                model=model_label(getattr(agent, "model", None)),
                gated=is_confirmation_gate(getattr(agent, "before_tool_callback", None)),
            )
        )
    return entries
