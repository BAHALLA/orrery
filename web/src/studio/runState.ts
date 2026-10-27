import type {
  A2aMessageEvent,
  ApprovalDecision,
  BudgetHitEvent,
  McpCallEvent,
  NodeOutcome,
  RunOutcome,
  RunSummary,
  StudioEvent,
  TokenCount,
  Totals,
} from "./types";

/**
 * A run as the console draws it, folded from its trace events. Pure.
 *
 * {@link reduceRun} is idempotent per `seq`: a reconnect that resumes with
 * `Last-Event-ID` may still replay an event the console already has (a frame
 * that arrived as the connection dropped), and it is skipped. Unknown event
 * types are kept in the timeline and otherwise ignored, so a newer server's
 * events never break an older console.
 */

/**
 * `awaiting`: a human approval waiting for its person. `unknown`: the node was
 * running when the console lost the run.
 */
export type NodeRunStatus = "pending" | "active" | "awaiting" | NodeOutcome | "unknown";

export interface NodeRun {
  id: string;
  status: NodeRunStatus;
  model: string | null;
  tokens: TokenCount;
  elapsedMs: number | null;
  /** Unix seconds of `node.enter`, for a live elapsed counter. */
  enteredAt: number | null;
  mcpCalls: number;
  a2aCalls: number;
  /** The node's answer, when the server captures content. */
  content: string | null;
}

/** One call to a remote agent: the request that left and the reply that came back. */
export interface A2aExchange {
  /** `<seq of the outbound event>`: stable across reconnects. */
  key: string;
  nodeId: string;
  remote: string;
  skill: string;
  outbound: A2aMessageEvent | null;
  inbound: A2aMessageEvent | null;
}

/** One decision the run asked a person for, from its `approval.*` events. */
export interface ApprovalAsk {
  approvalId: string;
  nodeId: string;
  prompt: string;
  digest: string;
  size: number;
  /** Unix seconds; undecided by then, it expires. */
  expiresAt: number;
  /** `null` while it waits. */
  decision: ApprovalDecision | null;
  /** The text under review, when the server captures content. */
  content: string | null;
}

/**
 * `lost`: the console can no longer read the run (its stream and its summary
 * are gone: it ended long ago, or lives on another replica), so how it ended
 * is not known. Final, like an outcome.
 */
export type RunStatus = "starting" | "running" | RunOutcome | "lost";

export interface RunView {
  runId: string;
  status: RunStatus;
  specName: string | null;
  specDigest: string | null;
  pattern: string | null;
  /** Node ids in run order, from `run.start`. */
  order: string[];
  nodes: Record<string, NodeRun>;
  /** The node running now. */
  active: string | null;
  budgets: Record<string, number>;
  events: StudioEvent[];
  lastSeq: number;
  /** Unix seconds of the first and last event. */
  startedAt: number | null;
  endedAt: number | null;
  budgetHit: BudgetHitEvent | null;
  totals: Totals | null;
  error: string | null;
  droppedEvents: number;
  mcp: McpCallEvent[];
  a2a: A2aExchange[];
  approvals: ApprovalAsk[];
  input: string | null;
  output: string | null;
}

const ZERO: TokenCount = { input: 0, output: 0, total: 0 };

export function initialRun(runId: string): RunView {
  return {
    runId,
    status: "starting",
    specName: null,
    specDigest: null,
    pattern: null,
    order: [],
    nodes: {},
    active: null,
    budgets: {},
    events: [],
    lastSeq: 0,
    startedAt: null,
    endedAt: null,
    budgetHit: null,
    totals: null,
    error: null,
    droppedEvents: 0,
    mcp: [],
    a2a: [],
    approvals: [],
    input: null,
    output: null,
  };
}

function blankNode(id: string): NodeRun {
  return {
    id,
    status: "pending",
    model: null,
    tokens: ZERO,
    elapsedMs: null,
    enteredAt: null,
    mcpCalls: 0,
    a2aCalls: 0,
    content: null,
  };
}

function patchNode(view: RunView, id: string, patch: (n: NodeRun) => Partial<NodeRun>): RunView {
  const node = view.nodes[id] ?? blankNode(id);
  return {
    ...view,
    order: view.order.includes(id) ? view.order : [...view.order, id],
    nodes: { ...view.nodes, [id]: { ...node, ...patch(node) } },
  };
}

/** Pair an inbound message with the latest outbound one of the same call still open. */
function addA2a(list: A2aExchange[], event: A2aMessageEvent): A2aExchange[] {
  // A reply pairs with the oldest unanswered request from the same node to
  // the same remote skill. With content capture on, the reply only arrives
  // when the tool call returns, so other events (MCP calls, even another
  // request) can sit between the two; the only order the server promises is
  // request before reply before the node's exit.
  if (event.direction === "inbound") {
    const i = list.findIndex(
      (x) =>
        x.inbound === null &&
        x.outbound !== null &&
        x.nodeId === event.node_id &&
        x.remote === event.remote &&
        x.skill === event.skill,
    );
    if (i >= 0) return list.map((y, j) => (j === i ? { ...y, inbound: event } : y));
  }
  const exchange: A2aExchange = {
    key: String(event.seq),
    nodeId: event.node_id,
    remote: event.remote,
    skill: event.skill,
    outbound: event.direction === "outbound" ? event : null,
    inbound: event.direction === "inbound" ? event : null,
  };
  return [...list, exchange];
}

export function reduceRun(view: RunView, event: StudioEvent): RunView {
  if (typeof event.seq === "number" && event.seq <= view.lastSeq) return view;
  // A finished run takes nothing but its run.end: the totals, when the
  // outcome came first from the run's summary (see applySummary).
  if (isFinished(view) && (event.type !== "run.end" || view.totals !== null)) return view;
  let next: RunView = {
    ...view,
    events: [...view.events, event],
    lastSeq: typeof event.seq === "number" ? event.seq : view.lastSeq,
    startedAt: view.startedAt ?? event.ts ?? null,
    endedAt: event.ts ?? view.endedAt,
  };
  if (next.status === "starting") next = { ...next, status: "running" };

  switch (event.type) {
    case "run.start": {
      const nodes = { ...next.nodes };
      for (const id of event.nodes) nodes[id] ??= blankNode(id);
      return {
        ...next,
        specName: event.spec_name,
        specDigest: event.spec_digest,
        pattern: event.pattern,
        order: event.nodes,
        nodes,
        budgets: event.budgets,
        input: event.content ?? null,
      };
    }
    case "node.enter":
      return {
        ...patchNode(next, event.node_id, () => ({
          status: "active",
          model: event.model,
          enteredAt: event.ts,
        })),
        active: event.node_id,
      };
    case "node.exit":
      return {
        ...patchNode(next, event.node_id, () => ({
          status: event.outcome,
          model: event.model,
          tokens: event.tokens,
          elapsedMs: event.elapsed_ms,
          content: event.content ?? null,
        })),
        active: next.active === event.node_id ? null : next.active,
      };
    case "mcp.call":
      return {
        ...patchNode(next, event.node_id, (n) => ({ mcpCalls: n.mcpCalls + 1 })),
        mcp: [...next.mcp, event],
      };
    case "a2a.message":
      return {
        ...(event.direction === "outbound"
          ? patchNode(next, event.node_id, (n) => ({ a2aCalls: n.a2aCalls + 1 }))
          : next),
        a2a: addA2a(next.a2a, event),
      };
    case "approval.requested":
      return {
        ...patchNode(next, event.node_id, () => ({ status: "awaiting" })),
        approvals: [
          ...next.approvals,
          {
            approvalId: event.approval_id,
            nodeId: event.node_id,
            prompt: event.prompt,
            digest: event.digest,
            size: event.size,
            expiresAt: event.expires_at,
            decision: null,
            content: event.content ?? null,
          },
        ],
      };
    case "approval.decided":
      return {
        ...patchNode(next, event.node_id, (n) => ({
          status: n.status === "awaiting" ? "active" : n.status,
        })),
        approvals: next.approvals.map((a) =>
          a.approvalId === event.approval_id ? { ...a, decision: event.decision } : a,
        ),
      };
    case "budget.hit":
      return { ...next, budgetHit: event };
    case "run.end": {
      // A node still marked active when the run ends stopped with it.
      const nodes = Object.fromEntries(
        Object.entries(next.nodes).map(([id, n]) => [
          id,
          n.status === "active" || n.status === "awaiting"
            ? { ...n, status: outcomeOfNode(event.outcome) }
            : n,
        ]),
      ) as Record<string, NodeRun>;
      return {
        ...next,
        status: event.outcome,
        nodes,
        active: null,
        totals: event.totals,
        error: event.error ?? null,
        droppedEvents: event.dropped_events ?? 0,
        output: event.content ?? null,
      };
    }
    default:
      return next;
  }
}

function outcomeOfNode(outcome: RunOutcome): NodeOutcome {
  return outcome === "succeeded" ? "ok" : outcome;
}

function settleActive(view: RunView, status: NodeRunStatus): Record<string, NodeRun> {
  return Object.fromEntries(
    Object.entries(view.nodes).map(([id, n]) => [
      id,
      n.status === "active" || n.status === "awaiting" ? { ...n, status } : n,
    ]),
  );
}

/** The decisions the run waits for now, oldest first. */
export function waitingApprovals(view: RunView): ApprovalAsk[] {
  return isFinished(view) ? [] : view.approvals.filter((a) => a.decision === null);
}

/**
 * Fold `GET /studio/runs/{id}` into a run whose stream was lost: when the
 * summary says it ended, the run takes that outcome (and the totals, which
 * the summary carries); while it is still running, nothing changes.
 */
export function applySummary(view: RunView, summary: RunSummary): RunView {
  if (summary.status === "running" || isFinished(view)) return view;
  return {
    ...view,
    status: summary.status,
    nodes: settleActive(view, outcomeOfNode(summary.status)),
    active: null,
    totals: summary.totals ?? view.totals,
    error: summary.error ?? view.error,
    droppedEvents: summary.dropped_events,
    endedAt: summary.ended_at ?? view.endedAt,
  };
}

/** The run can't be read any more: its outcome is unknown, and it is over for the console. */
export function markLost(view: RunView): RunView {
  if (isFinished(view)) return view;
  return { ...view, status: "lost", nodes: settleActive(view, "unknown"), active: null };
}

export function isFinished(view: RunView): boolean {
  return view.status !== "starting" && view.status !== "running";
}

/** Tokens so far: the totals once the run ended, else the sum of finished nodes. */
export function tokensSoFar(view: RunView): TokenCount {
  if (view.totals) return view.totals.tokens;
  return Object.values(view.nodes).reduce<TokenCount>(
    (sum, n) => ({
      input: sum.input + n.tokens.input,
      output: sum.output + n.tokens.output,
      total: sum.total + n.tokens.total,
    }),
    ZERO,
  );
}

/**
 * How much of each budget is used, as far as the events tell: tokens, tool
 * calls (MCP calls plus remote calls), remote calls, and wall time. Delegations
 * are only in the totals. Exact once `run.end` arrives.
 */
export function budgetUse(
  view: RunView,
  now: number,
): { name: string; used: number; limit: number }[] {
  const remote = view.a2a.filter((x) => x.outbound !== null).length;
  const used: Record<string, number> = view.totals
    ? {
        max_tokens: view.totals.tokens.total,
        max_tool_calls: view.totals.tool_calls,
        max_delegations: view.totals.delegations,
        max_remote_calls: view.totals.remote_calls,
        timeout_s: Math.round(view.totals.wall_ms / 1000),
      }
    : {
        max_tokens: tokensSoFar(view).total,
        max_tool_calls: view.mcp.length + remote,
        max_remote_calls: remote,
        timeout_s: view.startedAt ? Math.max(0, Math.round(now - view.startedAt)) : 0,
      };
  if (view.budgetHit) used[view.budgetHit.budget] = view.budgetHit.used;
  return Object.entries(view.budgets)
    .filter(([name]) => used[name] !== undefined)
    .map(([name, limit]) => ({ name, used: used[name] ?? 0, limit }));
}
