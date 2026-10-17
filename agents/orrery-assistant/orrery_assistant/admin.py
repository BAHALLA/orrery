"""Admin-editable agent set for the chat root (``/admin/catalog``).

Implements core's :class:`~orrery_core.serving.agent_admin.AgentAdmin` for this
application: what an admin may switch or compose, and how to build the
coordinator for a given catalog.

The deployment's env stays the ceiling. Only specialists the env allows can be
switched off (an env-excluded one is listed as unavailable), and declarative
agents from ``ORRERY_AGENTS_CONFIG`` are fixed — the catalog adds its own beside
them and may not reuse their names.
"""

from __future__ import annotations

from typing import Any

from orrery_core.agent.catalog_store import AgentCatalog
from orrery_core.agent.declarative import (
    DeclarativeAgentsFile,
    build_declarative_agents,
    tool_catalogue,
)
from orrery_core.registry import installed_specialists, source_of
from orrery_core.security.guardrails import get_guard_level, get_guard_reason
from orrery_core.serving.agent_admin import AdminOptions, SpecialistOption, ToolOption

from . import agent as chat


def _guard(tool: Any) -> str:
    level = get_guard_level(tool)
    return "read" if level is None else level


class ChatAgentAdmin:
    """The coordinator's catalog: switch specialists, compose declarative agents."""

    def __init__(self) -> None:
        # Env-allowed specialists: the ceiling, and the tool catalogue's source.
        self._allowed = list(chat._specialists)
        self._allowed_keys = {s.key for s in self._allowed}
        self._file_agents = list(chat._declarative_agents)

    def options(self) -> AdminOptions:
        # Excluded specialists are listed from package metadata only: opening
        # the Agents tab must not import code the deployment chose not to run.
        loaded = {s.key: s for s in self._allowed}
        catalogue, ambiguous = tool_catalogue(self._allowed)
        tools = [
            ToolOption(
                ref=ref,
                specialist=ref.split(".", 1)[0] if "." in ref else _owner(ref, self._allowed),
                guard=_guard(tool),
                reason=get_guard_reason(tool),
            )
            for ref, tool in sorted(catalogue.items())
            # Offer each tool once: the bare name when unique, else qualified.
            if "." not in ref or ref.split(".", 1)[1] in ambiguous
        ]
        return AdminOptions(
            specialists=[
                SpecialistOption(
                    key=i.key,
                    agent_name=s.agent.name if s else "",
                    description=(s.agent.description or "") if s else "",
                    source=(source_of(s.agent.name) if s else None) or i.source,
                    available=s is not None,
                )
                for i in installed_specialists()
                for s in [loaded.get(i.key)]
            ],
            tools=tools,
            file_agents=[a.name for a in self._file_agents],
        )

    def build(self, catalog: AgentCatalog) -> Any:
        disabled = set(catalog.disabled_specialists)
        if unknown := disabled - self._allowed_keys:
            raise ValueError(
                f"cannot switch specialists the deployment does not run: {sorted(unknown)}"
            )
        active = [s for s in self._allowed if s.key not in disabled]
        extra = build_declarative_agents(
            DeclarativeAgentsFile(agents=list(catalog.agents)),
            # Tools come from every env-allowed specialist, switched off or not:
            # a narrow agent over a broad specialist's tools is a common reason
            # to switch the broad one off.
            self._allowed,
            reserved_names=[*chat.RESERVED_AGENT_NAMES, *(a.name for a in self._file_agents)],
        )
        return chat.build_chat_agent(active, [*self._file_agents, *extra])


def _owner(tool_name: str, specialists: list[Any]) -> str:
    for s in specialists:
        for t in getattr(s.agent, "tools", None) or []:
            if (getattr(t, "name", None) or getattr(t, "__name__", "")) == tool_name:
                return s.key
    return ""
