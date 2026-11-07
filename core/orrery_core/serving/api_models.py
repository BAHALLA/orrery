"""Request and response models of the HTTP front door (``server.py``).

They are the API's wire format, so the console's types in ``web/src/api/types.ts``
follow them. ``server`` imports every one, so ``from orrery_core.serving.server
import MeResponse`` keeps working.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..agent.catalog_store import AgentCatalog
from ..agent.declarative import DeclarativeAgentConfig
from .mission import Mission


class ChatResponse(BaseModel):
    session_id: str
    response: str


class InventoryTool(BaseModel):
    name: str
    #: read | confirm | destructive | dynamic (an MCP toolset, classified per
    #: tool at run time).
    guard: str
    reason: str = ""


class InventoryAgent(BaseModel):
    name: str
    description: str
    #: builtin | plugin | declarative | mcp
    source: str
    tools: list[InventoryTool]
    #: The entry-point key; empty for declarative and MCP agents.
    key: str = ""
    #: The installed package that registered it, and its version.
    package: str = ""
    version: str = ""
    #: The model it calls (``a → b`` for a fallback chain).
    model: str = ""
    #: Whether a confirmation gate guards its tools.
    gated: bool = False


class DeploymentInfo(BaseModel):
    """What is running, for the admin view: versions, model, and selection."""

    app_name: str
    orrery_version: str
    adk_version: str
    python_version: str
    #: The coordinator's model, as configured.
    model: str
    model_provider: str
    autonomy_level: str | None
    admin_editing: bool
    #: ORRERY_SPECIALISTS / ORRERY_DISABLED_SPECIALISTS as set (empty: unset).
    enabled_specialists: str
    disabled_specialists: str
    #: When this process started serving (epoch seconds).
    started_at: float


class AgentsResponse(BaseModel):
    agents: list[InventoryAgent]
    deployment: DeploymentInfo | None = None


class CatalogSpecialist(BaseModel):
    key: str
    agent_name: str
    description: str
    source: str
    available: bool


class CatalogTool(BaseModel):
    ref: str
    specialist: str
    guard: str
    reason: str = ""


class CatalogResponse(BaseModel):
    editing_enabled: bool
    catalog: AgentCatalog
    #: The version this replica is serving (may briefly trail the store).
    applied_version: int
    specialists: list[CatalogSpecialist]
    tools: list[CatalogTool]
    file_agents: list[str]
    #: Names a composed agent may not take, so the console refuses them as typed.
    reserved_names: list[str] = []


class CatalogUpdate(BaseModel):
    #: The version the edit was made from — the optimistic-concurrency check.
    version: int
    disabled_specialists: list[str] = Field(default_factory=list)
    agents: list[DeclarativeAgentConfig] = Field(default_factory=list)


class SessionSummary(BaseModel):
    """One of the caller's conversations, as the history list needs it."""

    session_id: str
    #: Empty when nothing labelled the session (see ``_CONVERSATION_TITLE_KEY``);
    #: the client supplies its own placeholder rather than the server inventing one.
    title: str
    #: Unix seconds of the last write, for ordering and "3h ago" labels.
    last_update_time: float


class SessionsResponse(BaseModel):
    sessions: list[SessionSummary]


class SessionMessage(BaseModel):
    """One user or assistant message from a stored transcript."""

    role: str
    text: str
    #: Unix seconds.
    at: float


class SessionResponse(BaseModel):
    """A single conversation, transcript included."""

    session_id: str
    title: str
    messages: list[SessionMessage]
    last_update_time: float


class ActivityEntry(BaseModel):
    """One recorded tool call (shape written by ``ActivityPlugin``)."""

    operation: str
    details: str
    timestamp: str


class ActivityResponse(BaseModel):
    session_id: str
    entries: list[ActivityEntry]


class PendingConfirmationInfo(BaseModel):
    """The caller's guarded action awaiting an approve/deny decision."""

    #: Addresses the action for ``POST /confirmations/{action_id}/decision``.
    action_id: str
    tool_name: str
    level: str
    args: dict[str, object]
    created_at: float
    #: The conversation that raised it, so a client can show the card there
    #: rather than in every conversation. ``None`` when unknown (a pending
    #: raised outside a gateway turn).
    session_id: str | None = None


class PendingResponse(BaseModel):
    pending: PendingConfirmationInfo | None


class DecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]


class DecisionResponse(BaseModel):
    action_id: str
    decision: str


class TriageResponse(BaseModel):
    """Latest triage verdict recorded in this session (AEP-019 Milestone 2)."""

    session_id: str
    severity: str | None
    report: str | None


class AttachmentsInfo(BaseModel):
    """The attachment limits a client should check before uploading."""

    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    images: bool
    #: Image types accepted (the server re-checks by content, not by this).
    image_types: list[str]


class SpecialistInfo(BaseModel):
    """A specialist the coordinator can delegate to, for the console's copy."""

    #: Entry-point key (``kafka``), or the agent name when it has none.
    key: str
    #: Agent name (``kafka_health_agent``).
    name: str
    label: str | None = None


class LearningInfo(BaseModel):
    """A learner's place on the hint ladder (AEP-032 §3). Never a value."""

    scenario_id: str
    level: int
    max_level: int
    #: Whether this caller may take a hint: only a verified person.
    can_take_hint: bool


class MeResponse(BaseModel):
    """Who the caller is and what this deployment will let them do.

    The role is the server's own resolution, not the browser's reading of the
    token — the console decodes the JWT for display, but this is the value RBAC
    will actually enforce, so the two can be compared.
    """

    subject: str
    role: str
    #: ``"L2"``/``"L3"``/``"L4"``, or ``null`` when the autonomy gate is off.
    autonomy_level: str | None
    model_provider: str
    model_name: str
    #: Whether this deployment can run the onboarding self-test.
    self_test_available: bool
    #: What a chat turn may attach, or ``null`` when attachments are off.
    attachments: AttachmentsInfo | None = None
    #: The specialists the coordinator reaches right now (after
    #: ``ORRERY_SPECIALISTS`` and catalog edits); empty for a root that
    #: delegates to none. ``null`` only from servers older than this field,
    #: which the console reads as "the stock set". Display only.
    specialists: list[SpecialistInfo] | None = None
    #: Whether this caller may use the Studio: it is mounted
    #: (``ORRERY_STUDIO_ENABLED``) and the caller is a human admin. The
    #: console shows its entry only then; ``/studio/*`` checks it again.
    studio_available: bool = False
    #: The learning sandbox's state for this caller, or ``null`` outside it.
    learning: LearningInfo | None = None
    #: What visitors to this console are here to do, or ``null``. Display only.
    mission: Mission | None = None


class CheckResultModel(BaseModel):
    """One integration or connectivity check."""

    name: str
    label: str
    ok: bool
    detail: str
    hint: str
    duration_ms: int


class SelfTestResponse(BaseModel):
    """Result of the first-run environment check (AEP-019 Milestone 3)."""

    ok: bool
    checks: list[CheckResultModel]
