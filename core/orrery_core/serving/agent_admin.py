"""The contract between the HTTP admin endpoints and an application's agent tree.

Core serves ``/admin/catalog`` but cannot know how a particular application
assembles its coordinator, so the application supplies an :class:`AgentAdmin`:
what an admin may choose from, and how to build the agent tree for a catalog.
``build`` doubles as validation — the server builds the new tree *before* it
saves anything, so a catalog that cannot be built is refused with nothing
changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..agent.catalog_store import AgentCatalog


@dataclass(frozen=True)
class SpecialistOption:
    key: str
    agent_name: str
    description: str
    source: str
    #: False when the deployment's env (ORRERY_SPECIALISTS / _DISABLED_)
    #: excludes it: shown, but not switchable from the UI.
    available: bool = True


@dataclass(frozen=True)
class ToolOption:
    #: What a declarative agent lists: bare name, or ``key.tool`` when shared.
    ref: str
    specialist: str
    guard: str
    reason: str = ""


@dataclass(frozen=True)
class AdminOptions:
    specialists: list[SpecialistOption] = field(default_factory=list)
    tools: list[ToolOption] = field(default_factory=list)
    #: Declarative agents from ORRERY_AGENTS_CONFIG — managed in that file,
    #: read-only here. Their names are reserved.
    file_agents: list[str] = field(default_factory=list)
    #: Names the application keeps for itself (its coordinator, its own tools):
    #: ``build`` refuses a composed agent called one of them.
    reserved_names: list[str] = field(default_factory=list)


class AgentAdmin(Protocol):
    def options(self) -> AdminOptions: ...

    def build(self, catalog: AgentCatalog) -> Any:
        """The root agent for *catalog*. Raises ``ValueError`` when it can't be built."""
        ...
