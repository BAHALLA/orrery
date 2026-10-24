import { tokensSoFar, type RunView } from "./runState";
import type { CompositionSpec } from "./types";

/**
 * This session's runs, and comparing two of them.
 *
 * In memory for now: runs live on the server for an hour and only on the
 * replica that started them (P0a), so there is nothing to list yet. P0b stores
 * compositions, runs and events in Postgres; a {@link RunHistory} backed by
 * that API then replaces {@link MemoryHistory} without the panel changing.
 */

export interface RunRecord {
  runId: string;
  /** The spec exactly as it was sent: what this run is a run *of*. */
  spec: CompositionSpec;
  input: string;
  /** Browser clock, ms. */
  startedAt: number;
  view: RunView;
}

export interface RunHistory {
  list(): RunRecord[];
  put(record: RunRecord): RunHistory;
}

/** Newest first, capped, immutable (each `put` returns a new history). */
export class MemoryHistory implements RunHistory {
  private readonly records: RunRecord[];
  private readonly cap: number;

  constructor(records: RunRecord[] = [], cap = 20) {
    this.records = records;
    this.cap = cap;
  }

  list(): RunRecord[] {
    return this.records;
  }

  put(record: RunRecord): MemoryHistory {
    const rest = this.records.filter((r) => r.runId !== record.runId);
    const at = this.records.findIndex((r) => r.runId === record.runId);
    const next = at === -1 ? [record, ...rest] : [...rest.slice(0, at), record, ...rest.slice(at)];
    return new MemoryHistory(next.slice(0, this.cap), this.cap);
  }
}

export interface CompareRow {
  label: string;
  a: number | null;
  b: number | null;
  /** `b - a`, when both are known. */
  delta: number | null;
  unit: "tokens" | "ms" | "count";
}

function row(
  label: string,
  unit: CompareRow["unit"],
  a: number | null,
  b: number | null,
): CompareRow {
  return { label, unit, a, b, delta: a !== null && b !== null ? b - a : null };
}

/** Totals side by side, then each node's tokens and latency (nodes of either run, in order). */
export function compareRuns(a: RunView, b: RunView): { totals: CompareRow[]; nodes: CompareRow[] } {
  const wall = (v: RunView) =>
    v.totals?.wall_ms ??
    (v.startedAt !== null && v.endedAt !== null
      ? Math.round((v.endedAt - v.startedAt) * 1000)
      : null);
  const totals = [
    row("Tokens", "tokens", tokensSoFar(a).total, tokensSoFar(b).total),
    row("Input tokens", "tokens", tokensSoFar(a).input, tokensSoFar(b).input),
    row("Output tokens", "tokens", tokensSoFar(a).output, tokensSoFar(b).output),
    row("Tool calls", "count", a.totals?.tool_calls ?? null, b.totals?.tool_calls ?? null),
    row("Remote calls", "count", a.totals?.remote_calls ?? null, b.totals?.remote_calls ?? null),
    row("Delegations", "count", a.totals?.delegations ?? null, b.totals?.delegations ?? null),
    row("Wall time", "ms", wall(a), wall(b)),
  ];
  const ids = [...a.order, ...b.order.filter((id) => !a.order.includes(id))];
  const nodes = ids.flatMap((id) => {
    const x = a.nodes[id];
    const y = b.nodes[id];
    return [
      row(`${id} · tokens`, "tokens", x ? x.tokens.total : null, y ? y.tokens.total : null),
      row(`${id} · latency`, "ms", x?.elapsedMs ?? null, y?.elapsedMs ?? null),
    ];
  });
  return { totals, nodes };
}
