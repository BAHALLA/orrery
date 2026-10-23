import type { ProgressEvent } from "../api/types";
import { specialistFor, specialistLabel, titleCase } from "../specialists";

/** One tool call as the trace shows it. */
export interface TraceCall {
  callId: string;
  agent: string;
  tool: string;
  delegation: boolean;
  guarded: boolean;
  /**
   * `awaiting`: a gate paused the call for the caller's approval (it may still
   * run). `refused`: a gate said no — autonomy level, role, a run budget, an
   * open circuit — and the tool never ran. The server reports both as outcome
   * `refused`; the step's `status` tells them apart (see {@link stateOf}).
   */
  state: "running" | "ok" | "error" | "awaiting" | "refused";
  status: string;
  elapsedMs: number | null;
  /** Calls made *inside* this delegation, in start order. */
  children: TraceCall[];
}

/** A turn's live trace: top-level calls, each delegation holding its own. */
export type Trace = TraceCall[];

function fromStart(step: ProgressEvent): TraceCall {
  return {
    callId: step.call_id,
    agent: step.agent,
    tool: step.tool,
    delegation: step.delegation,
    guarded: step.guarded,
    state: "running",
    status: "",
    elapsedMs: null,
    children: [],
  };
}

/** The running delegation (searched depth-first) that a call by `agent` belongs to. */
function findParent(calls: TraceCall[], agent: string): TraceCall | null {
  for (let i = calls.length - 1; i >= 0; i--) {
    const call = calls[i];
    if (!call.delegation) continue;
    const nested = findParent(call.children, agent);
    if (nested) return nested;
    if (call.tool === agent && call.state === "running") return call;
  }
  return null;
}

function mapCall(calls: TraceCall[], id: string, fn: (c: TraceCall) => TraceCall): TraceCall[] {
  return calls.map((c) =>
    c.callId === id
      ? fn(c)
      : c.children.length
        ? { ...c, children: mapCall(c.children, id, fn) }
        : c,
  );
}

function insertUnder(calls: TraceCall[], parentId: string, child: TraceCall): TraceCall[] {
  return calls.map((c) =>
    c.callId === parentId
      ? { ...c, children: [...c.children, child] }
      : c.children.length
        ? { ...c, children: insertUnder(c.children, parentId, child) }
        : c,
  );
}

/** Gate answers that mean "paused for your approval", not "no". Mirrors
 * `STATUS_AWAITING_CONFIRMATION` (autonomy plugin) and `confirmation_required`
 * (guardrails) server-side. */
const AWAITING_STATUSES = new Set(["AWAITING_CONFIRMATION", "confirmation_required"]);

/** The finished state of a call, from its `end` step. */
export function stateOf(step: ProgressEvent): TraceCall["state"] {
  const outcome = step.outcome ?? "ok";
  if (outcome === "refused" && AWAITING_STATUSES.has(step.status)) return "awaiting";
  return outcome;
}

/**
 * Why a gate refused a call, in words an operator can act on.
 *
 * The statuses are the gates' own (`_NEUTRAL_STATUSES` in
 * core/orrery_core/reliability/resilience.py). An open circuit arrives as a
 * plain `error` status with outcome `refused`, because the breaker's
 * `error_type` is not forwarded. A refusal is the platform doing its job — at
 * autonomy L2 it is the expected answer to any write — so the wording explains
 * rather than alarms.
 */
export function refusalReason(status: string): string {
  switch (status) {
    case "BLOCKED":
      return "not allowed at this autonomy level";
    case "access_denied":
    case "denied":
      return "your role can't run this";
    case "CALL_BUDGET_EXHAUSTED":
      return "this run's call budget is spent";
    case "DELEGATION_BUDGET_EXHAUSTED":
      return "this specialist was asked too many times";
    case "REPEATED_FAILURE":
      return "same failure repeated, not retried";
    case "error":
    case "circuit_open":
    case "CircuitOpen":
    case "CircuitHalfOpen":
      return "circuit open, backing off";
    default:
      return status ? status.replace(/_/g, " ").toLowerCase() : "refused by a guard";
  }
}

/**
 * Fold one progress step into the trace. Pure — returns a new trace.
 *
 * A call is nested under the running delegation whose specialist made it (the
 * server reports the calling agent's name, and a delegation's `tool` *is* that
 * name). Anything else — the root's own calls — stays top level. An `end`
 * whose `start` was never seen (a stream joined late) is added as finished.
 */
export function applyProgress(trace: Trace, step: ProgressEvent): Trace {
  if (step.phase === "start") {
    const call = fromStart(step);
    const parent = findParent(trace, step.agent);
    return parent ? insertUnder(trace, parent.callId, call) : [...trace, call];
  }
  const finish = (c: TraceCall): TraceCall => ({
    ...c,
    state: stateOf(step),
    status: step.status,
    elapsedMs: step.elapsed_ms,
  });
  if (!containsCall(trace, step.call_id)) {
    return applyProgress(trace, { ...step, phase: "start" }).map((c) =>
      c.callId === step.call_id ? finish(c) : c,
    );
  }
  return mapCall(trace, step.call_id, finish);
}

function containsCall(calls: TraceCall[], id: string): boolean {
  return calls.some((c) => c.callId === id || containsCall(c.children, id));
}

/** Every call in the trace, depth-first. */
export function flatten(trace: Trace): TraceCall[] {
  return trace.flatMap((c) => [c, ...flatten(c.children)]);
}

/** Names the stock specialists' agents have gone by, for a server that
 * doesn't list its specialists (and for the root's own sub-agents). */
const FALLBACK_LABELS: ReadonlyArray<[RegExp, string]> = [
  [/^kafka/, "Kafka"],
  [/^k8s/, "Kubernetes"],
  [/^elasticsearch|^es_/, "Elasticsearch"],
  [/^observability/, "Observability"],
  [/^docker/, "Docker"],
  [/^ops_journal|^journal/, "Ops journal"],
  [/^incident_triage/, "Incident triage"],
];

/**
 * "kafka_health_agent" → "Kafka". The deployment's own specialist list (from
 * `/me`) wins, so a plugin specialist shows by its label; then the stock
 * names; anything else becomes "Title case words".
 */
export function agentLabel(name: string): string {
  const known = specialistFor(name);
  if (known) return specialistLabel(known);
  for (const [match, label] of FALLBACK_LABELS) if (match.test(name)) return label;
  return titleCase(name);
}

/** A one-line summary for a finished trace, e.g. "Kafka, Kubernetes · 7 calls". */
export function summarize(trace: Trace): string {
  const delegations = flatten(trace).filter((c) => c.delegation);
  const tools = flatten(trace).filter((c) => !c.delegation);
  const parts: string[] = [];
  if (delegations.length) {
    parts.push([...new Set(delegations.map((d) => agentLabel(d.tool)))].join(", "));
  }
  parts.push(`${tools.length} tool call${tools.length === 1 ? "" : "s"}`);
  const all = flatten(trace);
  const awaiting = all.filter((c) => c.state === "awaiting").length;
  const refused = all.filter((c) => c.state === "refused").length;
  if (awaiting) parts.push(`${awaiting} awaiting approval`);
  if (refused) parts.push(`${refused} refused`);
  return parts.join(" · ");
}
