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

The models describe every node kind the AEP plans, so a console can be built
against one schema. P0b runs ``llm_agent``, ``router``, ``parallel``/``join``
and ``supervisor`` nodes, labelled and looping edges, and per-node models;
:func:`validate_spec` refuses the rest (``remote_a2a``, ``human_approval``,
the ``handoff`` pattern) field by field, and checks the graph
(:func:`plan_graph`), before any model call.

The format is the one P0a published (``spec_version`` 1): P0b only lets
through fields that were already in it, and adds ``output`` to supervisors.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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

    @field_validator("labels")
    @classmethod
    def _unique(cls, labels: list[str]) -> list[str]:
        if len(set(labels)) != len(labels):
            raise ValueError("labels must be unique")
        return labels


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_ID, description="Unique in the spec; the compiled agent is n_<id>.")
    description: str = Field(default="", max_length=500)


class LlmAgentNode(_Node):
    """An Orrery agent (``create_agent()``): instruction, tools, optional model."""

    kind: Literal["llm_agent"]
    instruction: str = Field(min_length=1, max_length=8000)
    tools: list[str] = Field(default_factory=list, max_length=20)
    #: A model from ``ORRERY_STUDIO_MODELS``, run without the fallback chain.
    model: str | None = None
    #: Labels the node's answer is classified as; its edges are taken on them.
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
    """An llm_agent whose children (llm_agent or supervisor nodes) are its tools."""

    kind: Literal["supervisor"]
    instruction: str = Field(min_length=1, max_length=8000)
    children: list[str] = Field(min_length=1, max_length=10)
    tools: list[str] = Field(default_factory=list, max_length=20)
    model: str | None = None
    output: NodeOutput | None = None


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
    "human_approval": "P0c",
}
#: Patterns the console knows but the server cannot run yet.
UNSUPPORTED_PATTERNS = {"handoff": "P1; a router between agents does the same"}


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", pattern=_ID)
    to: str = Field(pattern=_ID)
    #: The source's output label this edge is taken on.
    when: str | None = None
    #: A back edge's cap: how many times the loop may go round.
    max_iterations: int | None = Field(default=None, ge=1, le=MAX_LOOP_ITERATIONS)
    #: Screen the text crossing this edge for prompt injection.
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
    """``POST /studio/runs``: a spec, or a stored composition (and version), and the input."""

    model_config = ConfigDict(extra="forbid")

    spec: dict[str, Any] | None = None
    composition_id: str | None = Field(default=None, min_length=1, max_length=64)
    #: The composition's version; unset, its latest.
    version: int | None = Field(default=None, ge=1)
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)

    @model_validator(mode="after")
    def _one_source(self) -> RunRequest:
        if (self.spec is None) == (self.composition_id is None):
            raise ValueError("give either spec or composition_id")
        if self.version is not None and self.composition_id is None:
            raise ValueError("version needs composition_id")
        return self


class CompositionWrite(BaseModel):
    """``POST``/``PUT /studio/compositions``: the spec, and (PUT) the version it was edited from."""

    model_config = ConfigDict(extra="forbid")

    spec: dict[str, Any]
    version: int | None = Field(default=None, ge=1)


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
    #: ``ORRERY_STUDIO_MODELS``: the models a node may name.
    models: frozenset[str] = frozenset()


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
    """Every reason *spec* cannot run in this release, field by field."""
    errors: list[SpecError] = []
    if spec.pattern in UNSUPPORTED_PATTERNS:
        errors.append(
            SpecError(
                ("pattern",),
                f"pattern {spec.pattern!r} is not supported yet "
                f"({UNSUPPORTED_PATTERNS[spec.pattern]})",
                "not_supported_yet",
            )
        )

    ids: dict[str, int] = {}
    for i, node in enumerate(spec.nodes):
        if node.id in ids:
            errors.append(SpecError(("nodes", i, "id"), f"duplicate node id {node.id!r}"))
        ids.setdefault(node.id, i)
        if node.kind in UNSUPPORTED_KINDS:
            errors.append(
                SpecError(
                    ("nodes", i, "kind"),
                    f"node kind {node.kind!r} is not supported yet "
                    f"({UNSUPPORTED_KINDS[node.kind]})",
                    "not_supported_yet",
                )
            )
            continue
        if not isinstance(node, LlmAgentNode | SupervisorNode):
            continue
        if node.model is not None and node.model not in ctx.models:
            errors.append(
                SpecError(
                    ("nodes", i, "model"),
                    f"model {node.model!r} is not in ORRERY_STUDIO_MODELS"
                    + (f" ({', '.join(sorted(ctx.models))})" if ctx.models else " (empty)")
                    + "; leave it unset to use the deployment's model",
                    "model_not_allowed",
                )
            )
        if len(set(node.tools)) != len(node.tools):
            errors.append(SpecError(("nodes", i, "tools"), "duplicate tool references"))
        for j, ref in enumerate(node.tools):
            if (why := _tool_error(ref, ctx)) is not None:
                errors.append(SpecError(("nodes", i, "tools", j), why, "unknown_tool"))
        if len(set(node.tools)) == len(node.tools):
            errors.extend(_name_errors(i, node.tools, ctx))
            if isinstance(node, SupervisorNode):
                errors.extend(_child_name_errors(i, node, ctx))

    if len(ids) == len(spec.nodes):
        plan, graph_errors = plan_graph(spec)
        errors.extend(graph_errors)
        if plan is not None and not graph_errors:
            errors.extend(_check_pattern(spec, plan))
    errors.extend(_check_budgets(spec, ctx.ceilings))
    return errors


def _child_name_errors(i: int, node: SupervisorNode, ctx: ValidationContext) -> list[SpecError]:
    """A child reaches the supervisor's model as ``n_<id>``: it must not shadow a tool."""
    names = {model_tool_name(ref, ctx) for ref in node.tools}
    return [
        SpecError(
            ("nodes", i, "children", k),
            f"child {child!r} reaches the model as {NODE_TOOL_PREFIX + child!r}, "
            "the name of one of the node's tools",
            "tool_name_collision",
        )
        for k, child in enumerate(node.children)
        if NODE_TOOL_PREFIX + child in names
    ]


#: Compiled agents (and so a supervisor's child tools) are named ``n_<id>``.
NODE_TOOL_PREFIX = "n_"


# ── The graph ────────────────────────────────────────────────────────


@dataclass
class GraphPlan:
    """What :func:`plan_graph` found in a valid spec; the compiler builds from it."""

    start: str
    #: Graph nodes (not supervisor children), breadth-first from the start.
    order: list[str]
    #: Supervisor id -> its children, as listed.
    children: dict[str, list[str]]
    #: Child id -> its supervisor.
    parent: dict[str, str]
    #: Edge indexes with ``max_iterations`` (each goes back to an earlier node).
    back_edges: set[int]
    #: Node id -> indexes of its outgoing edges.
    out: dict[str, list[int]]
    #: Join id -> the parallel node whose branches it closes.
    join_of: dict[str, str]
    #: Router id -> the labelled node before it.
    router_source: dict[str, str]

    def labels(self, spec: CompositionSpec, node_id: str) -> list[str] | None:
        """The labels edges out of *node_id* are taken on (a router's: its source's)."""
        by_id = {n.id: n for n in spec.nodes}
        node = by_id[self.router_source.get(node_id, node_id)]
        output = getattr(node, "output", None)
        return list(output.labels) if output is not None else None


def _labels_of(node: Any) -> list[str] | None:
    output = getattr(node, "output", None)
    return list(output.labels) if output is not None else None


def plan_graph(spec: CompositionSpec) -> tuple[GraphPlan | None, list[SpecError]]:
    """Check the nodes and edges form a graph this release runs; the plan if they do.

    - **Supervisors**: children are ``llm_agent`` or ``supervisor`` nodes, each
      under one supervisor, never in an edge, without labels, at most
      :data:`MAX_SUPERVISOR_DEPTH` supervisors deep.
    - **Edges**: between graph nodes, no self-loops or duplicates. Only a
      ``parallel`` node fans out (2 to :data:`MAX_PARALLEL_BRANCHES` plain
      edges); a node with ``output.labels`` leaves on labelled edges (``when``)
      or on one edge to a ``router``; any other node has at most one successor.
    - **Loops**: an edge with ``max_iterations`` goes back to an earlier node
      and is taken on a label. Without it, no cycle.
    - **Fan-in**: each ``join`` closes exactly the branches of one ``parallel``
      node, its branches neither route nor loop, and a node after it combines
      them.
    - one start node, every node reachable.
    """
    errors: list[SpecError] = []
    by_id = {n.id: n for n in spec.nodes}
    index = {n.id: i for i, n in enumerate(spec.nodes)}

    # Supervisors and their children.
    children: dict[str, list[str]] = {}
    parent: dict[str, str] = {}
    for i, node in enumerate(spec.nodes):
        if not isinstance(node, SupervisorNode):
            continue
        children[node.id] = []
        for k, child in enumerate(node.children):
            loc = ("nodes", i, "children", k)
            target = by_id.get(child)
            if target is None:
                errors.append(SpecError(loc, f"no node {child!r}"))
            elif child == node.id or child in children[node.id]:
                errors.append(SpecError(loc, f"{child!r} is listed twice or is the node itself"))
            elif not isinstance(target, LlmAgentNode | SupervisorNode):
                errors.append(
                    SpecError(
                        loc,
                        f"a supervisor's children are llm_agent or supervisor nodes; "
                        f"{child!r} is a {target.kind}",
                    )
                )
            elif child in parent:
                errors.append(SpecError(loc, f"{child!r} is already a child of {parent[child]!r}"))
            else:
                parent[child] = node.id
                children[node.id].append(child)
                if target.output is not None:
                    errors.append(
                        SpecError(
                            ("nodes", index[child], "output"),
                            f"{child!r} answers its supervisor, so it cannot have output labels",
                        )
                    )
    for sup in children:
        depth, cursor, seen = 1, sup, {sup}
        while cursor in parent:
            cursor = parent[cursor]
            if cursor in seen:
                errors.append(
                    SpecError(("nodes", index[sup], "children"), "supervisors form a cycle")
                )
                break
            seen.add(cursor)
            depth += 1
        if depth > MAX_SUPERVISOR_DEPTH:
            errors.append(
                SpecError(
                    ("nodes", index[sup], "children"),
                    f"{sup!r} is {depth} supervisors deep; at most {MAX_SUPERVISOR_DEPTH}",
                    "too_deep",
                )
            )

    graph_nodes = [n.id for n in spec.nodes if n.id not in parent]
    out: dict[str, list[int]] = {n: [] for n in graph_nodes}
    into: dict[str, list[int]] = {n: [] for n in graph_nodes}
    seen_pairs: set[tuple[str, str]] = set()
    for j, edge in enumerate(spec.edges):
        bad = False
        for end, name in (("from", edge.from_), ("to", edge.to)):
            if name not in by_id:
                errors.append(SpecError(("edges", j, end), f"no node {name!r}"))
                bad = True
            elif name in parent:
                errors.append(
                    SpecError(
                        ("edges", j, end),
                        f"{name!r} is a child of {parent[name]!r}: children are its tools, "
                        "not steps of the graph",
                    )
                )
                bad = True
        if bad:
            continue
        if edge.from_ == edge.to:
            errors.append(SpecError(("edges", j), "an edge cannot loop on one node"))
            continue
        if (edge.from_, edge.to) in seen_pairs:
            errors.append(SpecError(("edges", j), f"duplicate edge {edge.from_}->{edge.to}"))
            continue
        seen_pairs.add((edge.from_, edge.to))
        out[edge.from_].append(j)
        into[edge.to].append(j)
    if errors:
        return None, errors

    back = {j for j, e in enumerate(spec.edges) if e.max_iterations is not None}

    # Routers: one labelled node before each, nothing else after that node.
    router_source: dict[str, str] = {}
    for name in graph_nodes:
        node = by_id[name]
        if node.kind != "router":
            continue
        incoming = [j for j in into[name] if j not in back]
        sources = [spec.edges[j].from_ for j in incoming]
        if len(sources) != 1 or _labels_of(by_id[sources[0]]) is None:
            errors.append(
                SpecError(
                    ("nodes", index[name]),
                    "a router follows exactly one node with output.labels; its edges are "
                    "taken on that node's labels",
                )
            )
            continue
        if len(out[sources[0]]) != 1:
            errors.append(
                SpecError(
                    ("nodes", index[sources[0]]),
                    f"{sources[0]!r} goes to router {name!r}, so it can have no other edge",
                )
            )
        router_source[name] = sources[0]

    # Outgoing edges, by the kind of their source.
    for name in graph_nodes:
        node = by_id[name]
        edges = out[name]
        labels = (
            _labels_of(by_id[router_source[name]]) if name in router_source else _labels_of(node)
        )
        to_router = len(edges) == 1 and by_id[spec.edges[edges[0]].to].kind == "router"
        whens: set[str] = set()
        for j in edges:
            edge = spec.edges[j]
            if edge.when is not None:
                if labels is None:
                    errors.append(
                        SpecError(
                            ("edges", j, "when"),
                            f"{name!r} has no output.labels: only a labelled node (or a "
                            "router after one) has edges taken on a label",
                        )
                    )
                elif edge.when not in labels:
                    errors.append(
                        SpecError(
                            ("edges", j, "when"),
                            f"{edge.when!r} is not one of {name!r}'s labels {labels}",
                        )
                    )
                elif edge.when in whens:
                    errors.append(
                        SpecError(
                            ("edges", j, "when"),
                            f"{name!r} already has an edge for {edge.when!r}: one per label",
                        )
                    )
                whens.add(edge.when)
            elif labels is not None and not (to_router and name not in router_source):
                errors.append(
                    SpecError(
                        ("edges", j, "when"),
                        f"{name!r} is labelled: each of its edges needs when (one of {labels})",
                    )
                )
            if edge.max_iterations is not None and edge.when is None:
                errors.append(
                    SpecError(
                        ("edges", j, "max_iterations"),
                        "a loop edge is taken on a label: give it when as well",
                    )
                )
        if node.kind == "parallel":
            if not 2 <= len(edges) <= MAX_PARALLEL_BRANCHES:
                errors.append(
                    SpecError(
                        ("nodes", index[name]),
                        f"a parallel node fans out to 2 to {MAX_PARALLEL_BRANCHES} nodes; "
                        f"{name!r} has {len(edges)}",
                        "too_many_branches" if len(edges) > MAX_PARALLEL_BRANCHES else "invalid",
                    )
                )
        elif labels is None and len(edges) > 1:
            errors.append(
                SpecError(
                    ("edges", edges[1], "from"),
                    f"{name!r} already has a successor: only a parallel node fans out, and "
                    "only a labelled node branches",
                )
            )
    if errors:
        return None, errors

    # No cycle without max_iterations; each max_iterations edge goes back.
    forward: dict[str, list[str]] = {
        n: [spec.edges[j].to for j in out[n] if j not in back] for n in graph_nodes
    }
    state: dict[str, int] = {}

    def cyclic(n: str) -> bool:
        state[n] = 1
        for m in forward[n]:
            if state.get(m) == 1 or (m not in state and cyclic(m)):
                return True
        state[n] = 2
        return False

    if any(n not in state and cyclic(n) for n in graph_nodes):
        return None, [
            SpecError(
                ("edges",),
                "the edges form a cycle without max_iterations: a loop needs an edge back "
                f"with max_iterations (at most {MAX_LOOP_ITERATIONS})",
                "unbounded_cycle",
            )
        ]

    def reaches(a: str, b: str) -> bool:
        stack, seen = [a], set()
        while stack:
            n = stack.pop()
            if n == b:
                return True
            if n not in seen:
                seen.add(n)
                stack.extend(forward[n])
        return False

    for j in back:
        edge = spec.edges[j]
        if not reaches(edge.to, edge.from_):
            errors.append(
                SpecError(
                    ("edges", j, "max_iterations"),
                    f"max_iterations is for an edge back to an earlier node; {edge.to!r} does "
                    f"not lead to {edge.from_!r}",
                )
            )

    starts = [n for n in graph_nodes if not [j for j in into[n] if j not in back]]
    if len(starts) != 1:
        errors.append(
            SpecError(
                ("edges",),
                f"a composition has exactly one first node; these have no incoming edge: "
                f"{sorted(starts)}",
            )
        )
        return None, errors
    start = starts[0]
    if by_id[start].kind in ("router", "join"):
        errors.append(
            SpecError(("nodes", index[start], "kind"), f"a {by_id[start].kind} cannot come first")
        )

    order = [start]
    for n in order:
        for j in out[n]:
            if (m := spec.edges[j].to) not in order:
                order.append(m)
    for name in graph_nodes:
        if name not in order:
            errors.append(
                SpecError(("nodes", index[name], "id"), f"{name!r} is not reachable from the start")
            )
    if errors:
        return None, errors

    # Fan-in: each join closes one parallel's branches.
    join_of: dict[str, str] = {}
    in_branch: set[str] = set()

    def walk(n: str, par: str) -> str | None:
        """Follow one branch of *par* from *n* to its join."""
        while True:
            node = by_id[n]
            if node.kind == "join":
                return n
            in_branch.add(n)
            if node.kind == "parallel":
                inner = close(n)
                if inner is None:
                    return None
                n = inner
                continue
            if node.kind == "router" or _labels_of(node) is not None:
                errors.append(
                    SpecError(
                        ("nodes", index[n]),
                        f"{n!r} is on a branch of parallel {par!r}: a branch neither routes "
                        "nor loops (route after the join)",
                    )
                )
                return None
            nxt = [spec.edges[j].to for j in out[n]]
            if len(nxt) != 1:
                errors.append(
                    SpecError(
                        ("nodes", index[n]),
                        f"a branch of parallel {par!r} ends at {n!r} without reaching a join",
                    )
                )
                return None
            n = nxt[0]

    def close(par: str) -> str | None:
        ends: set[str] = set()
        joins: set[str] = set()
        for j in out[par]:
            first = spec.edges[j].to
            before = by_id[first]
            if before.kind == "join":
                errors.append(
                    SpecError(("edges", j), f"a branch of {par!r} needs a node before the join")
                )
                return None
            found = walk(first, par)
            if found is None:
                return None
            joins.add(found)
            ends.update(spec.edges[k].from_ for k in into[found])
        if len(joins) != 1:
            errors.append(
                SpecError(
                    ("nodes", index[par]),
                    f"the branches of {par!r} must meet at one join; they reach {sorted(joins)}",
                )
            )
            return None
        (join,) = joins
        if join in join_of and join_of[join] != par:
            errors.append(
                SpecError(("nodes", index[join]), f"{join!r} already closes {join_of[join]!r}")
            )
            return None
        if len(into[join]) != len(out[par]):
            errors.append(
                SpecError(
                    ("nodes", index[join]),
                    f"{join!r} waits for every incoming edge: it needs one from each of "
                    f"{par!r}'s {len(out[par])} branches and nothing else",
                )
            )
            return None
        join_of[join] = par
        nxt = out[join]
        if len(nxt) != 1:
            errors.append(
                SpecError(
                    ("nodes", index[join]),
                    f"{join!r} needs one node after it to combine the branches",
                )
            )
            return None
        return spec.edges[nxt[0]].to

    for name in order:
        if by_id[name].kind == "parallel" and name not in in_branch:
            close(name)
    for name in graph_nodes:
        if by_id[name].kind == "join" and name not in join_of and not errors:
            errors.append(
                SpecError(("nodes", index[name]), f"{name!r} does not close a parallel node")
            )
    for j in back:
        target = spec.edges[j].to
        if target in in_branch or target in join_of:
            errors.append(
                SpecError(
                    ("edges", j, "to"),
                    f"a loop cannot go back into a parallel branch ({target!r})",
                )
            )
    if errors:
        return None, errors
    return (
        GraphPlan(
            start=start,
            order=order,
            children=children,
            parent=parent,
            back_edges=back,
            out=out,
            join_of=join_of,
            router_source=router_source,
        ),
        [],
    )


#: What each pattern needs to be that pattern (``pattern`` is otherwise a hint).
_PATTERN_NEEDS: dict[str, tuple[str, Callable[[CompositionSpec, GraphPlan], bool]]] = {
    "router": ("a labelled edge (when)", lambda s, p: any(e.when for e in s.edges)),
    "critic_loop": ("an edge with max_iterations", lambda s, p: bool(p.back_edges)),
    "fan_out_fan_in": ("a parallel node and its join", lambda s, p: bool(p.join_of)),
    "map_reduce": ("a parallel node and its join", lambda s, p: bool(p.join_of)),
    "supervisor": ("a supervisor node", lambda s, p: bool(p.children)),
    "debate": (
        "parallel speakers or a loop between them",
        lambda s, p: bool(p.join_of or p.back_edges),
    ),
    "pipeline": (
        "no labels, fan-out or loops",
        lambda s, p: not (p.join_of or p.back_edges or any(e.when for e in s.edges)),
    ),
}


def _check_pattern(spec: CompositionSpec, plan: GraphPlan) -> list[SpecError]:
    need = _PATTERN_NEEDS.get(spec.pattern)
    if need is None or need[1](spec, plan):
        return []
    return [
        SpecError(
            ("pattern",),
            f"pattern {spec.pattern!r} needs {need[0]}; set the pattern that matches the graph",
            "pattern_mismatch",
        )
    ]


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


def graph_order(spec: CompositionSpec) -> list[str]:
    """Node ids, graph nodes breadth-first from the start, then supervisor children."""
    plan, _ = plan_graph(spec)
    if plan is None:
        return [n.id for n in spec.nodes]
    rest = [n.id for n in spec.nodes if n.id not in plan.order]
    return plan.order + rest
