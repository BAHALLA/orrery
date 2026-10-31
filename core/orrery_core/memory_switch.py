"""``ORRERY_LONG_TERM_MEMORY``: durable chats without lasting memory.

On by default. Set to ``false``, nothing carries a person's findings from one
session to the next, while sessions themselves may still be durable
(``DATABASE_URL``):

- no memory service and no ``MemoryPlugin`` (the builders skip them, and
  ``AgentGateway`` refuses one passed in anyway);
- no ``load_memory``/``preload_memory`` tool, and no tool marked
  :func:`~orrery_core.security.guardrails.cross_session` (``user:``/``app:``
  state, such as the ops journal's notes), anywhere in a served tree.

The learning sandbox (AEP-032) turns the same things off for its own reason;
this is the switch for a deployment that is not a sandbox (a lab's Studio
instance, a shared demo). The batch triage Workflow is not a conversation and
keeps its journal step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .env import env_flag

LONG_TERM_MEMORY_ENV = "ORRERY_LONG_TERM_MEMORY"


def long_term_memory_enabled() -> bool:
    """False when the deployment turned long-term memory off. Raises on a typo."""
    return env_flag(LONG_TERM_MEMORY_ENV, default=True)


def memory_safe_catalogue(catalogue: Mapping[str, Any]) -> dict[str, Any]:
    """*catalogue* without its ``cross_session`` tools when memory is off.

    For a picker such as Studio's: offering a tool the gateway would then
    refuse only moves the failure to the run.
    """
    from .learning_gate import is_active
    from .security.guardrails import is_cross_session

    keep_all = long_term_memory_enabled() and not is_active()
    return {
        name: tool for name, tool in catalogue.items() if keep_all or not is_cross_session(tool)
    }


#: ADK's memory tools: recall of earlier sessions by name.
RECALL_TOOL_NAMES = frozenset({"load_memory", "preload_memory"})

CROSS_SESSION_HINT = (
    "Remove what holds them: disable the specialist "
    "(e.g. ORRERY_DISABLED_SPECIALISTS=ops_journal), or drop the tools from a "
    "declarative agent (ORRERY_AGENTS_CONFIG or the console's Agents tab)"
)


def cross_session_reach(root: Any) -> list[str]:
    """``agent.tool`` for every recall or ``cross_session`` tool *root* can reach.

    Walks ``AgentTool`` specialists, declarative agents, sub-agents and the
    nodes of a graph ``Workflow``: ADK's memory tools, and tools marked
    :func:`~orrery_core.security.guardrails.cross_session` (``user:``/``app:``
    state, such as the ops journal's notes), which persist per person across
    sessions and, with ``DATABASE_URL``, across restarts.
    """
    from .security.guardrails import is_cross_session

    seen: set[int] = set()
    stack = [root]
    found: list[str] = []
    while stack:
        agent = stack.pop()
        if agent is None or id(agent) in seen:
            continue
        seen.add(id(agent))
        stack.extend(getattr(agent, "sub_agents", None) or [])
        graph = getattr(agent, "graph", None)
        stack.extend(getattr(graph, "nodes", None) or [])
        for tool in getattr(agent, "tools", None) or []:
            name = str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))
            if name in RECALL_TOOL_NAMES or is_cross_session(tool):
                found.append(f"{getattr(agent, 'name', '?')}.{name}")
            if (inner := getattr(tool, "agent", None)) is not None:
                stack.append(inner)
    return sorted(found)
