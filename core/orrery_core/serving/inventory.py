"""What the coordinator can actually reach: specialists, their tools, guard levels.

Read from the live agent tree rather than from configuration, so the admin view
cannot disagree with what is running — a specialist disabled by
``ORRERY_DISABLED_SPECIALISTS`` is absent here because it is absent there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_toolset import BaseToolset

from ..registry import source_of
from ..security.guardrails import get_guard_level, get_guard_reason

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


def agent_inventory(root: Any) -> list[AgentEntry]:
    """One entry per agent the root can delegate to. Empty for a graph root."""
    entries: list[AgentEntry] = []
    for tool in getattr(root, "tools", None) or []:
        if not isinstance(tool, AgentTool):
            continue
        agent = tool.agent
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
            )
        )
    return entries
