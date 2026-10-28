import { isFinished, tokensSoFar, type RunView } from "./runState";
import type { CompositionSpec } from "./types";

/**
 * The runs on show in this tab, and comparing two of them.
 *
 * Runs started here are put in as they start; past ones are put in when
 * opened from the server's list (`GET /studio/runs`), their events replayed.
 */

export interface RunRecord {
  runId: string;
  /** The composition's name, as the run reported it. */
  name: string;
  /**
   * The spec the run is a run *of*: exactly as sent for a run started here,
   * the saved version for one opened from the server, or null when that is
   * not known (a run of an unsaved canvas, from another tab).
   */
  spec: CompositionSpec | null;
  /** The saved composition version it ran, when it ran one. */
  source?: { compositionId: string; version: number } | undefined;
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
    // Over the cap, the oldest *finished* run goes: a run still going is
    // being followed, and dropping it would unlock the canvas mid-run.
    while (next.length > this.cap) {
      let drop = next.length - 1;
      while (drop >= 0 && !isFinished(next[drop].view)) drop -= 1;
      next.splice(drop === -1 ? next.length - 1 : drop, 1);
    }
    return new MemoryHistory(next, this.cap);
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
