"""The composition spec (AEP-031 §1): a versioned JSON document, never code.

A spec names nodes, the edges between them and the tools each node may use.
Tools are referenced by id only, never by import path:

- ``mcp:<entry>.<tool>`` — a tool of a registry MCP server, which must be
  classified ``read_only`` there;
- ``a2a:<entry>.<skill>`` — a registry remote agent's skill
  (:class:`~orrery_core.a2a.remote.GuardedRemoteA2aTool`);
- ``catalog:<ref>`` — a read-only tool of an installed specialist (the
  declarative-agent catalogue: ``catalog:get_consumer_lag``,
  ``catalog:k8s.get_events``).

The models describe every node kind the AEP plans (router, parallel/join,
supervisor in P0b; human approval in P0c), so a console can be built against
one schema. :func:`validate_spec` then refuses what this release cannot run,
field by field, before any model call.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..a2a.remote import jcs, safe_tool_name
from ..security.guardrails import get_guard_level
from .config import BudgetCeilings
from .registry import StudioRegistry

#: The newest spec version this server reads.
SPEC_VERSION = 1
#: Most nodes and parallel branches one composition may hold.
MAX_NODES = 30
MAX_PARALLEL_BRANCHES = 6
MAX_SUPERVISOR_DEPTH = 3
MAX_LOOP_ITERATIONS = 5
#: Longest run input, in characters (the console's message bound).
MAX_INPUT_CHARS = 8192

_ID = r"^[a-z][a-z0-9_]{0,30}$"
_NAME = r"^[a-z0-9][a-z0-9_-]{0,63}$"

Pattern = Literal[
    "pipeline",
    "fan_out_fan_in",
    "router",
    "supervisor",
    "critic_loop",
    "debate",
    "map_reduce",
    "handoff",
    "plan_and_execute",
]


class Budgets(BaseModel):
    """Whole-run limits (the composition ledger). Unset: the deployment's ceiling."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_delegations: int | None = Field(default=None, ge=1)
    max_remote_calls: int | None = Field(default=None, ge=1)
    timeout_s: int | None = Field(default=None, ge=1)

    def resolved(self, ceilings: BudgetCeilings) -> ResolvedBudgets:
        return ResolvedBudgets(
            **{
                name: getattr(self, name) or getattr(ceilings, name)
                for name in ResolvedBudgets.model_fields
            }
        )


class ResolvedBudgets(BaseModel):
    max_tokens: int
    max_tool_calls: int
    max_delegations: int
    max_remote_calls: int
    timeout_s: int


class NodeOutput(BaseModel):
    """What a node's answer is classified as, for labelled edges (P0b)."""

    model_config = ConfigDict(extra="forbid")

    labels: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")]] = Field(
        min_length=2, max_length=10
    )


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_ID, description="Unique in the spec; the compiled agent is n_<id>.")
    description: str = Field(default="", max_length=500)


class LlmAgentNode(_Node):
    """An Orrery agent (``create_agent()``): instruction, tools, optional model."""

    kind: Literal["llm_agent"]
    instruction: str = Field(min_length=1, max_length=8000)
    tools: list[str] = Field(default_factory=list, max_length=20)
    #: P0b: a model from ORRERY_STUDIO_MODELS, run without the fallback chain.
    model: str | None = None
    #: P0b: labels a router reads.
    output: NodeOutput | None = None


class RemoteA2aNode(_Node):
    """Console shorthand: an llm_agent holding one remote skill and told to forward (P1)."""

    kind: Literal["remote_a2a"]
    remote: str = Field(description="<entry>.<skill>")
    instruction: str = Field(default="", max_length=8000)


class RouterNode(_Node):
    """Reads the previous node's output label and picks an outgoing edge (P0b)."""

    kind: Literal["router"]


class ParallelNode(_Node):
    """Fans out to the nodes its edges name (P0b)."""

    kind: Literal["parallel"]


class JoinNode(_Node):
    """Waits for every incoming branch (P0b)."""

    kind: Literal["join"]


class SupervisorNode(_Node):
    """An llm_agent whose children are its tools (P0b)."""

    kind: Literal["supervisor"]
    instruction: str = Field(min_length=1, max_length=8000)
    children: list[str] = Field(min_length=1, max_length=10)
    tools: list[str] = Field(default_factory=list, max_length=20)
    model: str | None = None


class HumanApprovalNode(_Node):
    """Pauses the run until a human admin approves (P0c)."""

    kind: Literal["human_approval"]
    prompt: str = Field(min_length=1, max_length=2000)


Node = Annotated[
    LlmAgentNode
    | RemoteA2aNode
    | RouterNode
    | ParallelNode
    | JoinNode
    | SupervisorNode
    | HumanApprovalNode,
    Field(discriminator="kind"),
]

#: The phase that brings each node kind not runnable yet.
UNSUPPORTED_KINDS = {
    "remote_a2a": "P1; use an llm_agent with an a2a: tool",
    "router": "P0b",
    "parallel": "P0b",
    "join": "P0b",
    "supervisor": "P0b",
    "human_approval": "P0c",
}


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", pattern=_ID)
    to: str = Field(pattern=_ID)
    #: P0b: the source's output label this edge is taken on.
    when: str | None = None
    #: P0b: a back edge's iteration cap.
    max_iterations: int | None = Field(default=None, ge=1, le=MAX_LOOP_ITERATIONS)
    #: P0b: screen the text crossing this edge for prompt injection.
    screen: bool = False


class CompositionSpec(BaseModel):
    """A composition: nodes, edges, budgets. See the module docstring."""

    model_config = ConfigDict(extra="forbid")

    spec_version: int = Field(ge=1, le=SPEC_VERSION)
    name: str = Field(pattern=_NAME)
    description: str = Field(default="", max_length=1000)
    pattern: Pattern = "pipeline"
    budgets: Budgets = Field(default_factory=Budgets)
    nodes: list[Node] = Field(min_length=1, max_length=MAX_NODES)
    edges: list[Edge] = Field(default_factory=list, max_length=MAX_NODES * 3)

    def digest(self) -> str:
        """SHA-256 of the spec's canonical JSON (JCS)."""
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        return hashlib.sha256(jcs(data).encode("utf-8")).hexdigest()


class RunRequest(BaseModel):
    """``POST /studio/runs``: a spec (P0a) and the run's input text."""

    model_config = ConfigDict(extra="forbid")

    spec: dict[str, Any]
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)


def spec_json_schema() -> dict[str, Any]:
    """The published JSON Schema the console validates against."""
    return CompositionSpec.model_json_schema(by_alias=True)


# ── Validation ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpecError:
    """One field-level problem. ``loc`` is a JSON path into the spec."""

    loc: tuple[str | int, ...]
    msg: str
    code: str = "invalid"

    def as_dict(self) -> dict[str, Any]:
        return {"loc": list(self.loc), "msg": self.msg, "code": self.code}


class SpecInvalid(ValueError):
    """A spec that cannot run, with every problem found."""

    def __init__(self, errors: list[SpecError]) -> None:
        super().__init__("; ".join(f"{'.'.join(map(str, e.loc))}: {e.msg}" for e in errors))
        self.errors = errors


#: ``{from_version: upgrade}``; each returns the spec one version newer.
_UPGRADES: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}

_KIND_TAGS = {
    "llm_agent",
    "remote_a2a",
    "router",
    "parallel",
    "join",
    "supervisor",
    "human_approval",
}


def errors_from_validation(exc: ValidationError) -> list[SpecError]:
    errors = []
    for err in exc.errors():
        # Drop the discriminator tag pydantic puts in a union member's path.
        raw = err["loc"]
        loc = tuple(
            part
            for i, part in enumerate(raw)
            if not (i == 2 and raw[0] == "nodes" and part in _KIND_TAGS)
        )
        loc = tuple("from" if part == "from_" else part for part in loc)
        errors.append(SpecError(loc, err["msg"], err["type"]))
    return errors


def parse_spec(raw: Mapping[str, Any]) -> CompositionSpec:
    """*raw* upgraded to :data:`SPEC_VERSION` and parsed. Raises :class:`SpecInvalid`."""
    data = dict(raw)
    version = data.get("spec_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise SpecInvalid([SpecError(("spec_version",), "spec_version must be an integer")])
    if version > SPEC_VERSION:
        raise SpecInvalid(
            [
                SpecError(
                    ("spec_version",),
                    f"spec_version {version} is newer than this server reads ({SPEC_VERSION})",
                    "unsupported_version",
                )
            ]
        )
    while version < SPEC_VERSION:
        upgrade = _UPGRADES.get(version)
        if upgrade is None:
            raise SpecInvalid(
                [SpecError(("spec_version",), f"spec_version {version} is not known")]
            )
        data = upgrade(data)
        version = data["spec_version"]
    try:
        return CompositionSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecInvalid(errors_from_validation(exc)) from None


@dataclass(frozen=True)
class ToolRef:
    kind: Literal["mcp", "a2a", "catalog"]
    entry: str
    name: str

    @property
    def text(self) -> str:
        return f"{self.kind}:{self.entry}.{self.name}" if self.entry else f"catalog:{self.name}"


def parse_tool_ref(ref: str) -> ToolRef | None:
    kind, sep, rest = ref.partition(":")
    if not sep or not rest:
        return None
    if kind == "catalog":
        return ToolRef("catalog", "", rest)
    if kind in ("mcp", "a2a"):
        entry, dot, name = rest.partition(".")
        if dot and entry and name:
            return ToolRef(kind, entry, name)
    return None


@dataclass
class ValidationContext:
    """What a spec is checked against: the registry, the catalogue, the ceilings."""

    registry: StudioRegistry = field(default_factory=StudioRegistry)
    #: Catalogue ref -> tool. Only read-only tools are usable.
    catalogue: Mapping[str, Any] = field(default_factory=dict)
    ceilings: BudgetCeilings = field(default_factory=BudgetCeilings)


def _tool_error(ref: str, ctx: ValidationContext) -> str | None:
    parsed = parse_tool_ref(ref)
    if parsed is None:
        return (
            f"{ref!r} is not a tool reference: use mcp:<entry>.<tool>, "
            "a2a:<entry>.<skill> or catalog:<tool>"
        )
    if parsed.kind == "mcp":
        server = ctx.registry.mcp_entry(parsed.entry)
        if server is None:
            return f"no MCP server {parsed.entry!r} in the registry"
        if parsed.name not in server.tools:
            return f"MCP server {parsed.entry!r} does not list tool {parsed.name!r}"
        if parsed.name not in server.read_only:
            return (
                f"{parsed.name!r} is not classified read_only in the registry; "
                "Studio runs are read-only (L2)"
            )
        return None
    if parsed.kind == "a2a":
        remote = ctx.registry.a2a_entry(parsed.entry)
        if remote is None:
            return f"no remote agent {parsed.entry!r} in the registry"
        if parsed.name not in remote.skills:
            return f"remote agent {parsed.entry!r} is not registered for skill {parsed.name!r}"
        return None
    tool = ctx.catalogue.get(parsed.name)
    if tool is None:
        return f"no catalogue tool {parsed.name!r}"
    if get_guard_level(tool) is not None:
        return f"catalogue tool {parsed.name!r} changes state; Studio runs are read-only (L2)"
    return None


#: Longest function name the model providers accept.
MAX_TOOL_NAME = 64


def model_tool_name(ref: str, ctx: ValidationContext) -> str | None:
    """The function name the model will see for a valid *ref* (as the compiler names it)."""
    parsed = parse_tool_ref(ref)
    if parsed is None:
        return None
    if parsed.kind == "catalog":
        tool = ctx.catalogue.get(parsed.name)
        return str(getattr(tool, "name", parsed.name))
    if parsed.kind == "a2a":
        return safe_tool_name("a2a", parsed.entry, parsed.name)
    # ADK prefixes MCP tools itself, uncapped: <prefix>_<tool>.
    return f"{safe_tool_name('mcp', parsed.entry)}_{parsed.name}"


def _name_errors(i: int, tools: list[str], ctx: ValidationContext) -> list[SpecError]:
    """Two refs of one node must not reach the model under one name.

    Sanitising and the 64-character cap can map different refs to one name
    (``a2a:demo-writer.x`` and ``a2a:demo_writer.x``; two catalogue refs for one
    tool), and the model could then call neither reliably.
    """
    errors: list[SpecError] = []
    seen: dict[str, str] = {}
    for j, ref in enumerate(tools):
        if _tool_error(ref, ctx) is not None:
            continue  # already reported
        name = model_tool_name(ref, ctx)
        if name is None:
            continue
        if len(name) > MAX_TOOL_NAME:
            errors.append(
                SpecError(
                    ("nodes", i, "tools", j),
                    f"{ref!r} becomes the tool name {name!r}, over the {MAX_TOOL_NAME} "
                    "characters model providers accept",
                    "tool_name_too_long",
                )
            )
        elif name in seen:
            errors.append(
                SpecError(
                    ("nodes", i, "tools", j),
                    f"{ref!r} and {seen[name]!r} both reach the model as {name!r}",
                    "tool_name_collision",
                )
            )
        else:
            seen[name] = ref
    return errors


def validate_spec(spec: CompositionSpec, ctx: ValidationContext) -> list[SpecError]:
    """Every reason *spec* cannot run in this release (P0a), field by field."""
    errors: list[SpecError] = []
    if spec.pattern != "pipeline":
        errors.append(
            SpecError(
                ("pattern",),
                f"pattern {spec.pattern!r} is not supported yet (P0b); this release runs "
                "'pipeline' only",
                "not_supported_yet",
            )
        )

    ids: dict[str, int] = {}
    for i, node in enumerate(spec.nodes):
        if node.id in ids:
            errors.append(SpecError(("nodes", i, "id"), f"duplicate node id {node.id!r}"))
        ids.setdefault(node.id, i)
        if not isinstance(node, LlmAgentNode):
            errors.append(
                SpecError(
                    ("nodes", i, "kind"),
                    f"node kind {node.kind!r} is not supported yet "
                    f"({UNSUPPORTED_KINDS[node.kind]})",
                    "not_supported_yet",
                )
            )
            continue
        if node.model is not None:
            errors.append(
                SpecError(
                    ("nodes", i, "model"),
                    "per-node models are not supported yet (P0b); leave it unset to use "
                    "the deployment's model",
                    "not_supported_yet",
                )
            )
        if node.output is not None:
            errors.append(
                SpecError(
                    ("nodes", i, "output"),
                    "output labels are not supported yet (P0b)",
                    "not_supported_yet",
                )
            )
        if len(set(node.tools)) != len(node.tools):
            errors.append(SpecError(("nodes", i, "tools"), "duplicate tool references"))
        for j, ref in enumerate(node.tools):
            if (why := _tool_error(ref, ctx)) is not None:
                errors.append(SpecError(("nodes", i, "tools", j), why, "unknown_tool"))
        if len(set(node.tools)) == len(node.tools):
            errors.extend(_name_errors(i, node.tools, ctx))

    errors.extend(_check_pipeline(spec, ids))
    errors.extend(_check_budgets(spec, ctx.ceilings))
    return errors


def _check_pipeline(spec: CompositionSpec, ids: Mapping[str, int]) -> list[SpecError]:
    """The edges must form one chain through every node (P0a's only shape)."""
    errors: list[SpecError] = []
    succ: dict[str, str] = {}
    pred: dict[str, str] = {}
    for j, edge in enumerate(spec.edges):
        for end, name in (("from", edge.from_), ("to", edge.to)):
            if name not in ids:
                errors.append(SpecError(("edges", j, end), f"no node {name!r}"))
        for attr, phase in (("when", "P0b"), ("max_iterations", "P0b")):
            if getattr(edge, attr) is not None:
                errors.append(
                    SpecError(
                        ("edges", j, attr),
                        f"{attr} is not supported yet ({phase}): a pipeline has no "
                        "conditional or looping edges",
                        "not_supported_yet",
                    )
                )
        if edge.screen:
            errors.append(
                SpecError(
                    ("edges", j, "screen"),
                    "edge screening is not supported yet (P0b)",
                    "not_supported_yet",
                )
            )
        if edge.from_ == edge.to:
            errors.append(SpecError(("edges", j), "an edge cannot loop on one node"))
        elif edge.from_ in succ:
            errors.append(
                SpecError(
                    ("edges", j, "from"),
                    f"{edge.from_!r} already has a successor: a pipeline does not branch",
                )
            )
        elif edge.to in pred:
            errors.append(
                SpecError(
                    ("edges", j, "to"),
                    f"{edge.to!r} already has a predecessor: a pipeline does not join",
                )
            )
        else:
            succ[edge.from_] = edge.to
            pred[edge.to] = edge.from_
    if errors:
        return errors
    starts = [n for n in ids if n not in pred]
    if len(starts) != 1:
        return [
            SpecError(
                ("edges",),
                "a pipeline has exactly one first node; these have no incoming edge: "
                f"{sorted(starts)}"
                if starts
                else "the edges form a cycle, and cycles need max_iterations (P0b)",
            )
        ]
    seen = [starts[0]]
    while (nxt := succ.get(seen[-1])) is not None:
        if nxt in seen:
            return [SpecError(("edges",), "the edges form a cycle (P0b loops need max_iterations)")]
        seen.append(nxt)
    for name, i in ids.items():
        if name not in seen:
            errors.append(
                SpecError(("nodes", i, "id"), f"{name!r} is not reachable from the start")
            )
    return errors


def _check_budgets(spec: CompositionSpec, ceilings: BudgetCeilings) -> list[SpecError]:
    errors = []
    for name in ResolvedBudgets.model_fields:
        asked = getattr(spec.budgets, name)
        ceiling = getattr(ceilings, name)
        if asked is not None and asked > ceiling:
            errors.append(
                SpecError(
                    ("budgets", name),
                    f"{asked} exceeds this deployment's ceiling of {ceiling}",
                    "over_ceiling",
                )
            )
    return errors


def pipeline_order(spec: CompositionSpec) -> list[str]:
    """Node ids in run order (valid specs only)."""
    pred = {e.to for e in spec.edges}
    succ = {e.from_: e.to for e in spec.edges}
    order = [next(n.id for n in spec.nodes if n.id not in pred)]
    while (nxt := succ.get(order[-1])) is not None:
        order.append(nxt)
    return order
