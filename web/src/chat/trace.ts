import type { ProgressEvent } from "../api/types";

/** One tool call as the trace shows it. */
export interface TraceCall {
  callId: string;
  agent: string;
  tool: string;
  delegation: boolean;
  guarded: boolean;
  state: "running" | "ok" | "error" | "refused";
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
    state: step.outcome ?? "ok",
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

/** Human labels for the specialists an operator recognises. */
const AGENT_LABELS: ReadonlyArray<[RegExp, string]> = [
  [/^kafka/, "Kafka"],
  [/^k8s/, "Kubernetes"],
  [/^elasticsearch|^es_/, "Elasticsearch"],
  [/^observability/, "Observability"],
  [/^docker/, "Docker"],
  [/^ops_journal|^journal/, "Ops journal"],
  [/^incident_triage/, "Incident triage"],
];

/** "kafka_health_agent" → "Kafka"; unknown names become "Title case words". */
export function agentLabel(name: string): string {
  for (const [match, label] of AGENT_LABELS) if (match.test(name)) return label;
  const words = name.replace(/_(agent|specialist)$/, "").replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
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
  const refused = flatten(trace).filter((c) => c.state === "refused").length;
  if (refused) parts.push(`${refused} awaiting approval or refused`);
  return parts.join(" · ");
}
