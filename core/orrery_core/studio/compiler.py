"""Spec → ADK ``Workflow`` (AEP-031 §1).

Every ``llm_agent`` and ``supervisor`` node becomes a ``create_agent()``
agent, so every guard, plugin and confirmation hook applies. Agents are named
``n_<id>`` so they cannot collide with ADK's reserved names or the
catalogue's agents. Tools:

- ``catalog:<ref>``: the catalogue's own (reviewed, read-only) tool object;
- ``a2a:<entry>.<skill>``: a :class:`GuardedRemoteA2aTool`, named
  ``a2a_<entry>_<skill>``;
- ``mcp:<entry>.<tool>``: one :class:`GuardedMcpToolset` per entry and node,
  filtered to the referenced tools and prefixed ``mcp_<entry>``, so the
  model sees ``mcp_<entry>_<tool>``. Built per run, closed by
  :meth:`CompiledComposition.close` (the runner calls it in ``finally``);
- a supervisor's children: each an ``AgentTool`` over the child's agent,
  named ``n_<child>``.

The graph (see :func:`~.spec.plan_graph`):

- a node with ``output.labels`` answers with structured output
  (``{label, answer}``, its ``output_schema``), and a route ``FunctionNode``
  after it sets ``ctx.route`` to the label and passes ``answer`` on
  (``route.decision``). A ``router`` node is that function node;
- an edge with ``max_iterations`` gets a counter ``FunctionNode`` that lets
  the loop go round that many times, then ends the branch
  (``loop.iteration``), as ``verify_route`` does for remediation;
- a ``parallel`` node is a pass-through ``FunctionNode`` with one edge per
  branch, so ADK runs the branches concurrently; a ``join`` is ADK's
  ``JoinNode`` and a function node that lays the branches' answers out as one
  text for the node after it;
- an edge with ``screen`` gets a function node running the safety screen on
  the text crossing it;
- a ``human_approval`` node is an async function node that waits for the
  person's decision (:meth:`Trace.request_approval`, see :mod:`.approval`)
  and routes on ``approved`` / ``rejected`` like a labelled node. A rejection
  with no edge for it raises :class:`ApprovalRejected`, and the run ends
  ``rejected``.

A node's model is the deployment's (``None``: ``resolve_model()``) unless it
names one, which then runs alone, without the fallback chain.

Only call this on a spec :func:`~.spec.validate_spec` accepted.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from google.adk.agents.context import Context
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.workflow import FunctionNode, JoinNode, Workflow
from pydantic import BaseModel, Field, create_model

from ..a2a.remote import (
    GuardedRemoteA2aTool,
    RemoteCards,
    RemoteExchange,
    safe_tool_name,
)
from ..agent.prompts import OPERATING_PRINCIPLES
from ..plugins.safety_plugin import neutralize_text
from ..security.guardrails import get_guard_level, mark_confirmation_gate
from ..serving.inventory import model_label
from ..tools.mcp import GuardedMcpToolset
from .approval import ApprovalOutcome
from .events import LoopIteration, NodeEnter, NodeExit, RouteDecision, TokenCount
from .registry import StudioRegistry
from .spec import (
    APPROVAL_LABELS,
    NODE_TOOL_PREFIX,
    CompositionSpec,
    GraphPlan,
    HumanApprovalNode,
    LlmAgentNode,
    SupervisorNode,
    graph_order,
    parse_tool_ref,
    plan_graph,
)

logger = logging.getLogger("orrery.studio")

NODE_PREFIX = NODE_TOOL_PREFIX

#: Appended to every node's instruction.
STUDIO_RULES = """
## Studio node `{node_id}`
You are one step of a composition. Your input is what the step before you
handed on (or the run's input, for the first step). Answer with what the next
step needs, and nothing else. Remote agents and MCP tools see only what you
send them: include everything they need, and never send credentials or
personal data they do not need."""

LABEL_RULES = """
Classify your answer as exactly one of: {labels}. Reply with `label` set to it
and `answer` set to the text the next step should get."""

SUPERVISOR_RULES = """
Your helpers are tools ({children}): call one with a `request` that says
everything it needs, and use its answer. They see only that request."""

CHILD_RULES = """
You are a helper of `{parent}`: your input is its request, and your answer
goes back to it."""


@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    kind: str
    model: str
    #: The supervisor this node is a tool of, if any.
    parent: str | None = None
    #: Its output labels, if it is labelled.
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpToolInfo:
    node_id: str
    entry: str
    tool: str
    classification: str


class Trace(Protocol):
    """Where the graph's function nodes report: events, and the answer they pass on."""

    def emit_event(self, event: Any) -> None: ...

    def record_output(self, node_id: str, text: str) -> None: ...

    def answer_of(self, node_id: str) -> str | None: ...

    async def request_approval(self, node_id: str, prompt: str, text: str) -> ApprovalOutcome: ...

    async def request_input(self, node_id: str, remote: str, question: str) -> str | None:
        """A person's answer to a remote's *question*, or ``None`` (declined, expired)."""
        ...

    async def request_action(self, node_id: str, remote: str, request: str) -> bool:
        """Whether a person lets *node_id* send *request* to the acting *remote*."""
        ...


class RouteFailed(Exception):
    """A labelled node answered without one of its labels."""


class ApprovalRejected(Exception):
    """A person rejected (or let expire) an approval with no edge to take on it."""

    def __init__(self, node_id: str, outcome: ApprovalOutcome, detail: str = "") -> None:
        super().__init__(
            f"approval {node_id!r} "
            + ("expired" if outcome.decision == "expired" else "was rejected")
            + detail
        )
        self.node_id = node_id
        self.outcome = outcome


#: What a rejected branch gets: the reviewer's comment, then what was reviewed.
REJECTED_TEXT = """A reviewer rejected this at `{node_id}`.

Reviewer's comment: {comment}

What was reviewed:
{text}"""


@dataclass
class CompiledComposition:
    workflow: Workflow
    #: Agent name (``n_<id>``) -> node, for the agents (not the function nodes).
    nodes: dict[str, NodeInfo]
    #: ``(agent name, model-facing tool name)`` -> what it is. Keyed by the
    #: agent too: two nodes may use the same ``mcp_<entry>_<tool>``.
    mcp_tools: dict[tuple[str, str], McpToolInfo]
    order: list[str]
    toolsets: list[BaseToolset] = field(default_factory=list)
    #: Model-facing names of the acting remotes' tools: allowed at L2 for this
    #: run only, since :func:`confirmation_gate` asks the owner for each call.
    acting_tools: frozenset[str] = frozenset()

    async def close(self) -> None:
        for toolset in self.toolsets:
            try:
                await toolset.close()
            except Exception:  # noqa: BLE001 - one failed close must not keep the others open
                logger.warning("Closing a Studio toolset failed", exc_info=True)


ExchangeSink = Callable[[str, RemoteExchange], None]


class McpPins:
    """First-seen MCP tool fingerprints for the whole process, per registry entry.

    A Studio run builds its own toolsets, and a toolset's pin store would
    otherwise die with it: every run would pin whatever the server serves now,
    and a changed description would never be noticed. Like :class:`RemoteCards`,
    one of these lives as long as the process.
    """

    def __init__(self) -> None:
        self._by_entry: dict[str, dict[str, str]] = {}
        self._seen: dict[str, dict[str, dict[str, Any]]] = {}

    def for_entry(self, entry_id: str) -> dict[str, str]:
        return self._by_entry.setdefault(entry_id, {})

    def seen_for_entry(self, entry_id: str) -> dict[str, dict[str, Any]]:
        """Tool -> the description and input schema it was pinned with."""
        return self._seen.setdefault(entry_id, {})


def registry_toolset(
    server: Any, entry_id: str, names: list[str], *, cards: RemoteCards, pins: McpPins
) -> GuardedMcpToolset:
    """A registry MCP entry's toolset, filtered to *names* (one per node and run)."""
    prefix = safe_tool_name("mcp", entry_id)
    config = server.model_copy(
        update={
            "name": prefix,
            "tools": names,
            "read_only": [n for n in names if n in server.read_only],
            "destructive": [n for n in names if n in server.destructive],
            "pinned": {t: d for t, d in server.pinned.items() if t in names},
        }
    )
    return GuardedMcpToolset(
        config,
        private_allow=cards.network.private_allow,
        pin_store=pins.for_entry(entry_id),
        # Registry entries never name files: the deployment's CA bundle.
        verify=cards.network.ssl_context,
        egress_proxy=cards.network.egress_proxy,
        seen_store=pins.seen_for_entry(entry_id),
    )


class _NoTrace:
    def emit_event(self, event: Any) -> None:
        return None

    def record_output(self, node_id: str, text: str) -> None:
        return None

    def answer_of(self, node_id: str) -> str | None:
        return None

    async def request_approval(self, node_id: str, prompt: str, text: str) -> ApprovalOutcome:
        return ApprovalOutcome("rejected")  # nobody to ask: never approve

    async def request_input(self, node_id: str, remote: str, question: str) -> str | None:
        return None  # nobody to ask: the remote is sent nothing

    async def request_action(self, node_id: str, remote: str, request: str) -> bool:
        return False  # nobody to ask: nothing acts


#: What the model gets when a call that changes things was not confirmed.
NOT_CONFIRMED = {
    "status": "BLOCKED",
    "error": (
        "The person did not confirm this action, so it was not sent. Do not retry it; "
        "say what you would have done."
    ),
}


def confirmation_gate(sink: Trace) -> Callable[..., Any]:
    """The Studio's confirmation gate: the run's owner confirms every guarded call.

    Validation lets only one kind of guarded tool into a run: an acting remote
    (``effect: acting``, from a deployment that allows them). Its every call
    waits for the owner to confirm the exact request it would send (an
    approval of kind ``action``, against that text's digest); a reject, an
    expiry or a cancel means it is not sent. Any other guarded tool is
    refused outright. Marked as a gate, so ``create_agent()`` adds no other.
    """

    async def gate(*, tool: Any, args: dict[str, Any], tool_context: Any) -> dict | None:
        if get_guard_level(getattr(tool, "func", tool)) is None:
            return None
        if not isinstance(tool, GuardedRemoteA2aTool) or tool.entry.effect != "acting":
            return {
                "status": "BLOCKED",
                "error": f"{tool.name} changes things; in a Studio run only acting remotes may.",
            }
        request = args.get("request")
        if not isinstance(request, str) or not request.strip():
            return None  # the tool refuses it itself, before anything is sent
        agent = str(getattr(tool_context, "agent_name", "") or "")
        node_id = agent.removeprefix(NODE_PREFIX)
        if await sink.request_action(node_id, f"{tool.entry.id}.{tool.skill}", request):
            return None
        return dict(NOT_CONFIRMED)

    return mark_confirmation_gate(gate)


def _safe(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower())


def _judged_by(spec: CompositionSpec, plan: GraphPlan, source: str) -> str | None:
    """The node whose answer *source* (a labelled node, or its router) takes in."""
    labelled = plan.router_source.get(source, source)
    before = [
        spec.edges[k].from_
        for outs in plan.out.values()
        for k in outs
        if spec.edges[k].to == labelled and k not in plan.back_edges
    ]
    if len(before) != 1:
        return None
    return plan.router_source.get(before[0], before[0])


#: The structured-output model of a labelled node is ``Labelled_<node id>``.
LABELLED_PREFIX = "Labelled_"


def label_schema(node_id: str, labels: list[str]) -> type[BaseModel]:
    """The structured output of a labelled node: one of *labels*, and the answer."""
    return create_model(
        f"{LABELLED_PREFIX}{node_id}",
        label=(Literal[tuple(labels)], Field(description="One of the labels.")),  # ty: ignore[invalid-type-form]
        answer=(str, Field(description="The text the next step gets.")),
    )


def _text_of(node_input: Any) -> str:
    """The text a node hands on, whatever shape the graph passes it in."""
    if isinstance(node_input, str):
        return node_input
    if isinstance(node_input, BaseModel):
        node_input = node_input.model_dump()
    if isinstance(node_input, dict) and isinstance(node_input.get("answer"), str):
        return node_input["answer"]
    parts = getattr(node_input, "parts", None)
    if parts is not None:
        return "".join(p.text for p in parts if getattr(p, "text", None))
    return "" if node_input is None else str(node_input)


def _label_and_answer(node_input: Any) -> tuple[Any, str]:
    data = node_input.model_dump() if isinstance(node_input, BaseModel) else node_input
    if not isinstance(data, dict):
        return None, ""
    answer = data.get("answer")
    return data.get("label"), answer if isinstance(answer, str) else ""


def compile_spec(
    spec: CompositionSpec,
    *,
    registry: StudioRegistry,
    cards: RemoteCards,
    catalogue: Mapping[str, Any],
    model: Any = None,
    model_for: Callable[[str], Any] | None = None,
    on_exchange: ExchangeSink | None = None,
    mcp_pins: McpPins | None = None,
    trace: Trace | None = None,
) -> CompiledComposition:
    """Build the run's ``Workflow``.

    *model* is the deployment's (``None``: ``resolve_model()``); *model_for*
    builds a node's own model from its name (default ``resolve_model(name)``).
    """
    from ..agent.base import create_agent, resolve_model

    plan, errors = plan_graph(spec)
    if plan is None:
        raise ValueError(f"compile_spec needs a valid spec: {errors}")
    sink: Trace = trace or _NoTrace()
    gate = confirmation_gate(sink)
    acting: set[str] = set()
    default_model = model if model is not None else resolve_model()
    build_model = model_for or (lambda name: resolve_model(name))
    by_id = {n.id: n for n in spec.nodes}
    nodes: dict[str, NodeInfo] = {}
    mcp_tools: dict[tuple[str, str], McpToolInfo] = {}
    toolsets: list[BaseToolset] = []
    agents: dict[str, Any] = {}
    pins = mcp_pins or McpPins()

    def tools_of(node: LlmAgentNode | SupervisorNode) -> list[Any]:
        tools: list[Any] = []
        mcp_refs: dict[str, list[str]] = {}
        observer = (lambda exchange, _n=node.id: on_exchange(_n, exchange)) if on_exchange else None
        for ref in node.tools:
            parsed = parse_tool_ref(ref)
            assert parsed is not None
            if parsed.kind == "catalog":
                tools.append(catalogue[parsed.name])
            elif parsed.kind == "a2a":
                entry = registry.a2a_entry(parsed.entry)
                assert entry is not None
                remote = f"{parsed.entry}.{parsed.name}"
                if entry.effect == "acting":
                    acting.add(safe_tool_name("a2a", entry.id, parsed.name))
                tools.append(
                    GuardedRemoteA2aTool(
                        entry,
                        parsed.name,
                        cards=cards,
                        observer=observer,
                        # A remote's question goes to the run's owner, never the model.
                        ask=lambda q, _n=node.id, _r=remote: sink.request_input(_n, _r, q),
                    )
                )
            else:
                mcp_refs.setdefault(parsed.entry, []).append(parsed.name)
        for entry_id, names in mcp_refs.items():
            server = registry.mcp_entry(entry_id)
            assert server is not None
            prefix = safe_tool_name("mcp", entry_id)
            toolset = registry_toolset(server, entry_id, names, cards=cards, pins=pins)
            toolsets.append(toolset)
            tools.append(toolset)
            for name in names:
                mcp_tools[(NODE_PREFIX + node.id, f"{prefix}_{name}")] = McpToolInfo(
                    node_id=node.id,
                    entry=entry_id,
                    tool=name,
                    classification="read_only" if name in server.read_only else "destructive",
                )
        return tools

    def agent_for(node_id: str) -> Any:
        if node_id in agents:
            return agents[node_id]
        node = by_id[node_id]
        assert isinstance(node, LlmAgentNode | SupervisorNode)
        tools = tools_of(node)
        instruction = node.instruction + STUDIO_RULES.format(node_id=node_id)
        if isinstance(node, SupervisorNode):
            kids = plan.children[node_id]
            tools.extend(AgentTool(agent=agent_for(child)) for child in kids)
            instruction += SUPERVISOR_RULES.format(
                children=", ".join(f"`{NODE_PREFIX}{c}`" for c in kids)
            )
        if (parent := plan.parent.get(node_id)) is not None:
            instruction += CHILD_RULES.format(parent=parent)
        schema = None
        if node.output is not None:
            instruction += LABEL_RULES.format(labels=", ".join(node.output.labels))
            schema = label_schema(node_id, list(node.output.labels))
        node_model = build_model(node.model) if node.model else default_model
        agent = create_agent(
            name=NODE_PREFIX + node_id,
            description=node.description or f"Studio node {node_id}",
            instruction=f"{instruction}\n{OPERATING_PRINCIPLES}",
            tools=tools,
            model=node_model,
            output_schema=schema,
            before_tool_callback=gate,
        )
        agents[node_id] = agent
        nodes[agent.name] = NodeInfo(
            node_id=node_id,
            kind=node.kind,
            model=model_label(node_model),
            parent=parent,
            labels=tuple(node.output.labels) if node.output else (),
        )
        return agent

    edges: list[Any] = []
    entries: dict[str, Any] = {}
    exits: dict[str, Any] = {}

    def instant(node_id: str, kind: str, text: str | None = None) -> None:
        """node.enter and node.exit of a function node (no model, no tokens)."""
        sink.emit_event(NodeEnter(node_id=node_id, kind=kind, model=""))
        sink.emit_event(
            NodeExit(
                node_id=node_id,
                kind=kind,
                model="",
                tokens=TokenCount(),
                elapsed_ms=0,
                outcome="ok",
            )
        )
        if text is not None:
            sink.record_output(node_id, text)

    def route_node(node_id: str, source: str, name: str) -> FunctionNode:
        labels = plan.labels(spec, node_id) or []
        targets = {
            spec.edges[j].when: spec.edges[j].to
            for j in plan.out[node_id]
            if spec.edges[j].when is not None
        }
        router = node_id != source

        def route(ctx: Context, node_input: Any) -> str:
            label, answer = _label_and_answer(node_input)
            if label not in labels:
                raise RouteFailed(f"node {source!r} answered without one of its labels {labels}")
            if router:
                instant(node_id, "router")
            sink.emit_event(
                RouteDecision(
                    node_id=node_id,
                    source=source,
                    label=label,
                    alternatives=[x for x in labels if x != label],
                    to=targets.get(label),
                )
            )
            sink.record_output(source, answer)
            ctx.route = label
            return answer

        return FunctionNode(func=route, name=name)

    # Each loop counts per entry: taking an outer loop's edge again starts
    # the loops inside it over.
    loop_counts = dict.fromkeys(plan.back_edges, 0)

    def loop_node(j: int) -> FunctionNode:
        edge = spec.edges[j]
        cap = edge.max_iterations or 1
        state = loop_counts
        inner = plan.inner_loops(j)
        judged = _judged_by(spec, plan, edge.from_)
        # A person's rejection never turns into a result: past the cap, the
        # run ends rejected rather than with the draft they turned down.
        by_person = by_id[edge.from_].kind == "human_approval" and edge.when == "rejected"

        def loop(ctx: Context, node_input: Any) -> Any:
            state[j] += 1
            count = state[j]
            stopped = count > cap
            if not stopped:
                for k in inner:
                    state[k] = 0
            sink.emit_event(
                LoopIteration(
                    edge=f"{edge.from_}->{edge.to}",
                    from_node=edge.from_,
                    to_node=edge.to,
                    iteration=min(count, cap),
                    cap=cap,
                    stopped=stopped,
                )
            )
            if stopped and by_person:
                raise ApprovalRejected(
                    edge.from_,
                    ApprovalOutcome("rejected"),
                    f" {cap + 1} times, past its loop's cap",
                )
            ctx.route = "stop" if stopped else "continue"
            if stopped and judged is not None and (text := sink.answer_of(judged)) is not None:
                # No edge for "stop": the branch ends. Its answer is what the
                # source was judging (the last draft), not its verdict.
                sink.record_output(judged, text)
                return text
            return node_input

        return FunctionNode(func=loop, name=f"loop_{j}")

    def screen_node(j: int) -> FunctionNode:
        edge = spec.edges[j]

        def screen(node_input: Any) -> str:
            text, hits = neutralize_text(
                node_input if isinstance(node_input, str) else str(node_input)
            )
            if hits:
                logger.info(
                    "Studio edge %s->%s: %d instruction-like span(s) screened",
                    edge.from_,
                    edge.to,
                    hits,
                )
            return text

        return FunctionNode(func=screen, name=f"screen_{j}")

    def approval_node(node: HumanApprovalNode) -> FunctionNode:
        targets = {
            spec.edges[j].when: spec.edges[j].to
            for j in plan.out[node.id]
            if spec.edges[j].when is not None
        }

        async def approve(ctx: Context, node_input: Any) -> str:
            text = _text_of(node_input)
            sink.emit_event(NodeEnter(node_id=node.id, kind=node.kind, model=""))
            started = time.monotonic()
            try:
                outcome = await sink.request_approval(node.id, node.prompt, text)
            except BaseException as exc:
                # A cancel, the deadline or too many approvals: the enter
                # still gets its exit.
                sink.emit_event(
                    NodeExit(
                        node_id=node.id,
                        kind=node.kind,
                        model="",
                        tokens=TokenCount(),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        outcome="cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed",
                    )
                )
                raise
            label = "approved" if outcome.approved else "rejected"
            # Only a decision routes: an expiry is a reject that ends the run,
            # never a lap round a loop that would ask (and wait) again.
            ends = outcome.decision != "approved" and (
                outcome.decision != "rejected" or label not in targets
            )
            sink.emit_event(
                NodeExit(
                    node_id=node.id,
                    kind=node.kind,
                    model="",
                    tokens=TokenCount(),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    outcome="ok"
                    if outcome.approved
                    else "cancelled"
                    if outcome.decision == "cancelled"
                    else "rejected",
                )
            )
            if outcome.decision == "cancelled":
                raise asyncio.CancelledError  # the run is being cancelled: never "rejected"
            if ends:
                raise ApprovalRejected(node.id, outcome)
            sink.emit_event(
                RouteDecision(
                    node_id=node.id,
                    source=node.id,
                    label=label,
                    alternatives=[x for x in APPROVAL_LABELS if x != label],
                    to=targets.get(label),
                )
            )
            answer = (
                text
                if outcome.approved
                else REJECTED_TEXT.format(
                    node_id=node.id, comment=outcome.comment or "(none)", text=text
                )
            )
            sink.record_output(node.id, answer)
            ctx.route = label
            return answer

        return FunctionNode(func=approve, name=NODE_PREFIX + node.id)

    def fan_out_node(node_id: str) -> FunctionNode:
        def fan_out(node_input: Any) -> Any:
            instant(node_id, "parallel")
            return node_input

        return FunctionNode(func=fan_out, name=NODE_PREFIX + node_id)

    def combine_node(node_id: str, feeds: list[tuple[int, str, str]]) -> FunctionNode:
        def combine(node_input: Any) -> str:
            answers = node_input if isinstance(node_input, dict) else {}
            text = "\n\n".join(
                f"## {branch}\n{answers[name]}"
                for _j, name, branch in sorted(feeds)
                if name in answers
            )
            instant(node_id, "join", text)
            return text

        return FunctionNode(func=combine, name=NODE_PREFIX + node_id)

    # Graph nodes: what edges go into (entry) and come out of (exit).
    #: Join id -> ``(edge index, name of the node feeding it, branch node id)``.
    join_feeds: dict[str, list[tuple[int, str, str]]] = {}
    for node_id in plan.order:
        node = by_id[node_id]
        if isinstance(node, LlmAgentNode | SupervisorNode):
            agent = agent_for(node_id)
            entries[node_id] = agent
            to_router = any(by_id[spec.edges[j].to].kind == "router" for j in plan.out[node_id])
            if node.output is not None and not to_router:
                exits[node_id] = route_node(node_id, node_id, f"route_{node_id}")
                edges.append((agent, exits[node_id]))
            else:
                exits[node_id] = agent
        elif node.kind == "router":
            entries[node_id] = exits[node_id] = route_node(
                node_id, plan.router_source[node_id], NODE_PREFIX + node_id
            )
        elif node.kind == "parallel":
            entries[node_id] = exits[node_id] = fan_out_node(node_id)
        elif isinstance(node, HumanApprovalNode):
            entries[node_id] = exits[node_id] = approval_node(node)
        else:  # join
            barrier = JoinNode(name=f"join_{node_id}")
            entries[node_id] = barrier
            exits[node_id] = combine_node(node_id, join_feeds.setdefault(node_id, []))
            edges.append((barrier, exits[node_id]))

    def chain_to(j: int) -> Any:
        """What edge *j*'s source hands to: the loop counter, the screen, the target."""
        edge = spec.edges[j]
        target = entries[edge.to]
        head = target
        if edge.screen:
            screen = screen_node(j)
            edges.append((screen, target))
            head = screen
        if j in plan.back_edges:
            counter = loop_node(j)
            edges.append((counter, {"continue": head}))
            head = counter
        if by_id[edge.to].kind == "join":
            # The join's answers are keyed by the node that fed them.
            last = head if head is not target else exits[edge.from_]
            join_feeds[edge.to].append((j, last.name, edge.from_))
        return head

    edges.insert(0, ("START", entries[plan.start]))
    for node_id in plan.order:
        node = by_id[node_id]
        outs = plan.out[node_id]
        if not outs:
            continue
        source = exits[node_id]
        if isinstance(source, FunctionNode) and (
            node.kind in ("router", "human_approval") or source.name == f"route_{node_id}"
        ):
            edges.append((source, {spec.edges[j].when: chain_to(j) for j in outs}))
        elif node.kind == "parallel":
            edges.append((source, tuple(chain_to(j) for j in outs)))
        else:
            (j,) = outs
            edges.append((source, chain_to(j)))
    workflow = Workflow(
        name=f"studio_{_safe(spec.name)}",
        description=spec.description or f"Studio composition {spec.name}",
        edges=edges,
    )
    return CompiledComposition(
        workflow=workflow,
        nodes=nodes,
        mcp_tools=mcp_tools,
        order=graph_order(spec),
        toolsets=toolsets,
        acting_tools=frozenset(acting),
    )
