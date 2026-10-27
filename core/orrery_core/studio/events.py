"""Trace events v2 (AEP-031 §4): what a Studio run streams, and what a console renders.

Every event has ``type``, ``seq`` (1, 2, … within the run; the SSE ``id``),
``run_id`` and ``ts`` (Unix seconds). The union :data:`StudioEvent` is
published as JSON Schema at ``GET /studio/schema`` (``events``), so the web
console is built against this module, not against prose.

Content stays off the wire by default: names, ids, states, sizes, counts and
timings only. With ``ORRERY_STUDIO_CAPTURE_CONTENT=true`` some events carry
``content``: text after PII redaction (credentials and email addresses), cut
to :data:`MAX_CONTENT_BYTES`.

A run emits at most :data:`MAX_EVENTS_PER_RUN` events. Past
``MAX_EVENTS_PER_RUN - RESERVED_EVENTS``, everything but ``budget.hit``,
``run.end`` and the ``approval.*`` events is dropped, and ``run.end`` reports
how many were.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ..security.redaction import redact_text

MAX_EVENTS_PER_RUN = 2000
#: Approvals one run may ask for (``approval.py``): each is two kept events.
MAX_APPROVALS_PER_RUN = 12
#: Kept free for ``budget.hit``, ``run.end`` and every approval's two events.
RESERVED_EVENTS = 4 + 2 * MAX_APPROVALS_PER_RUN
MAX_CONTENT_BYTES = 2048

RunOutcome = Literal["succeeded", "failed", "cancelled", "budget_exhausted", "rejected"]
NodeOutcome = Literal["ok", "failed", "cancelled", "budget_exhausted", "rejected"]
BudgetName = Literal[
    "max_tokens", "max_tool_calls", "max_delegations", "max_remote_calls", "timeout_s"
]


def capture(text: str | None, *, redact_ips: bool = False) -> str | None:
    """*text* redacted (credentials, emails; IPv4 with *redact_ips*) and cut to
    :data:`MAX_CONTENT_BYTES` of UTF-8."""
    if not text:
        return None
    redacted, _ = redact_text(text, redact_emails=True, redact_ips=redact_ips)
    data = redacted.encode("utf-8")
    if len(data) <= MAX_CONTENT_BYTES:
        return redacted
    return data[: MAX_CONTENT_BYTES - 3].decode("utf-8", "ignore") + "…"


class TokenCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: int = 0
    output: int = 0
    total: int = 0


class Totals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: TokenCount = Field(default_factory=TokenCount)
    #: Model label (``gemini-2.5-flash``, ``a → b`` for a fallback chain) -> tokens.
    tokens_by_model: dict[str, TokenCount] = Field(default_factory=dict)
    tool_calls: int = 0
    delegations: int = 0
    remote_calls: int = 0
    wall_ms: int = 0


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = 0
    run_id: str = ""
    ts: float = 0.0


class RunStart(_Event):
    type: Literal["run.start"] = "run.start"
    spec_name: str
    spec_digest: str
    pattern: str
    #: Node ids: graph nodes breadth-first from the start, then supervisor children.
    nodes: list[str]
    budgets: dict[str, int]
    #: The stored composition the run came from, if any.
    composition_id: str | None = None
    composition_version: int | None = None
    content: str | None = None


class RunEnd(_Event):
    type: Literal["run.end"] = "run.end"
    outcome: RunOutcome
    #: A short reason when not ``succeeded`` (no content).
    error: str | None = None
    totals: Totals
    #: Events dropped past the per-run cap.
    dropped_events: int = 0
    content: str | None = None


class NodeEnter(_Event):
    type: Literal["node.enter"] = "node.enter"
    node_id: str
    kind: str
    #: The model label; empty for router, parallel and join nodes.
    model: str
    #: The supervisor that called this node as a tool (children only).
    parent: str | None = None


class NodeExit(_Event):
    type: Literal["node.exit"] = "node.exit"
    node_id: str
    kind: str
    model: str
    tokens: TokenCount
    elapsed_ms: int
    outcome: NodeOutcome
    parent: str | None = None
    content: str | None = None


class RouteDecision(_Event):
    """A labelled node's answer chose an edge (or ended its branch)."""

    type: Literal["route.decision"] = "route.decision"
    #: The labelled node, or the router after it.
    node_id: str
    #: The node whose answer carried the label (``node_id`` unless a router).
    source: str
    label: str
    alternatives: list[str]
    #: Where the label leads; ``None``: no edge, the branch ends here.
    to: str | None = None


class LoopIteration(_Event):
    """A loop edge was taken (``stopped=False``) or refused at its cap (``stopped=True``)."""

    type: Literal["loop.iteration"] = "loop.iteration"
    #: ``"<from>-><to>"``.
    edge: str
    from_node: str
    to_node: str
    #: Times the loop has gone round, this one included (at the cap: the cap).
    iteration: int
    cap: int
    stopped: bool = False


class PartSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    size: int


class A2aMessage(_Event):
    type: Literal["a2a.message"] = "a2a.message"
    node_id: str
    remote: str
    skill: str
    direction: Literal["outbound", "inbound"]
    method: str
    task_id: str | None = None
    context_id: str | None = None
    task_state: str | None = None
    #: Task states seen during the exchange, in order (inbound only).
    states: list[str] = Field(default_factory=list)
    parts: list[PartSummary] = Field(default_factory=list)
    elapsed_ms: int | None = None
    #: The tool's result status (inbound), or ``refused`` when nothing was sent.
    status: str | None = None
    card_digest: str | None = None
    content: str | None = None


class McpCall(_Event):
    type: Literal["mcp.call"] = "mcp.call"
    node_id: str
    entry: str
    tool: str
    #: ``read_only`` or the guard level the tool carries.
    classification: str
    #: ``success`` / ``failure`` / ``refused`` (a gate or budget answered).
    outcome: str
    status: str | None = None
    elapsed_ms: int | None = None


class ApprovalRequested(_Event):
    """A run paused for a person's decision (P0c). The text is served apart, to its owner."""

    type: Literal["approval.requested"] = "approval.requested"
    node_id: str
    approval_id: str
    #: ``approval`` (a ``human_approval`` node), ``input`` (a remote's
    #: question) or ``action`` (a call to an acting remote, before it is sent).
    kind: Literal["approval", "input", "action"]
    #: The question: the spec's ``prompt`` for an approval, Orrery's own words
    #: naming the remote for an input (never a model's or a remote's text).
    prompt: str
    #: The remote (``<entry>.<skill>``), for an ``input`` or an ``action``.
    remote: str | None = None
    #: SHA-256 of the text under review; approving names it.
    digest: str
    size: int
    #: Unix seconds; undecided by then, the approval expires (a reject).
    expires_at: float
    content: str | None = None


class ApprovalDecided(_Event):
    type: Literal["approval.decided"] = "approval.decided"
    node_id: str
    approval_id: str
    kind: Literal["approval", "input", "action"]
    decision: Literal["approved", "rejected", "expired", "cancelled"]
    waited_ms: int
    #: The reviewer's comment (capture on only). An answer to a remote is
    #: captured as the outbound ``a2a.message`` it becomes.
    content: str | None = None


class BudgetHit(_Event):
    type: Literal["budget.hit"] = "budget.hit"
    budget: BudgetName
    limit: int
    used: int


StudioEvent = Annotated[
    RunStart
    | RunEnd
    | NodeEnter
    | NodeExit
    | RouteDecision
    | LoopIteration
    | A2aMessage
    | McpCall
    | ApprovalRequested
    | ApprovalDecided
    | BudgetHit,
    Field(discriminator="type"),
]

EventAdapter: TypeAdapter[Any] = TypeAdapter(StudioEvent)

#: Events never dropped by the per-run cap: a person must see what waits for them.
ALWAYS_KEPT = frozenset({"run.end", "budget.hit", "approval.requested", "approval.decided"})


def events_json_schema() -> dict[str, Any]:
    return EventAdapter.json_schema()
