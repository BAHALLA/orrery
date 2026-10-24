import type {
  A2aMessageEvent,
  BudgetHitEvent,
  McpCallEvent,
  NodeOutcome,
  RunOutcome,
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

export type NodeRunStatus = "pending" | "active" | NodeOutcome;

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

export type RunStatus = "starting" | "running" | RunOutcome;

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
  if (event.direction === "inbound") {
    for (let i = list.length - 1; i >= 0; i--) {
      const x = list[i];
      if (
        x.inbound === null &&
        x.nodeId === event.node_id &&
        x.remote === event.remote &&
        x.skill === event.skill
      ) {
        return list.map((y, j) => (j === i ? { ...y, inbound: event } : y));
      }
    }
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
    case "budget.hit":
      return { ...next, budgetHit: event };
    case "run.end": {
      // A node still marked active when the run ends stopped with it.
      const nodes = Object.fromEntries(
        Object.entries(next.nodes).map(([id, n]) => [
          id,
          n.status === "active" ? { ...n, status: outcomeOfNode(event.outcome) } : n,
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
