import { useEffect, useState } from "react";
import { tonePill } from "../components/severity";
import { describeApiError } from "../system/useSystem";
import type { StudioClient } from "./api";
import {
  ago,
  formatCount,
  formatMs,
  panelCard,
  runLabel,
  runTone,
  sectionTitle,
  subtleButton,
} from "./format";
import type { RunStatus } from "./runState";
import type { RunSummary } from "./types";

/**
 * The caller's runs as the server keeps them (`GET /studio/runs`): this
 * replica's and the stored ones, newest first. Opening one replays its events
 * into the Run tab, or follows it again when it is still going here.
 */
export function SavedRuns({
  client,
  compositionId,
  openedIds,
  locked,
  liveRunId,
  onOpen,
}: {
  client: StudioClient;
  /** The saved composition on the canvas: its runs can be listed alone. */
  compositionId: string | null;
  /** Runs already in this tab, shown as such. */
  openedIds: ReadonlySet<string>;
  /** A run is live (or starting): only it may be opened, nothing else may replace the canvas. */
  locked: boolean;
  liveRunId: string | null;
  onOpen: (summary: RunSummary) => void;
}) {
  const [onlyThis, setOnlyThis] = useState(false);
  const filter = onlyThis && compositionId ? compositionId : undefined;
  // Each fetch is keyed by what it asked for; loading is "the answer on show
  // is for another key", so nothing is set synchronously in the effect.
  const [tick, setTick] = useState(0);
  const key = `${filter ?? ""}#${tick}`;
  const [result, setResult] = useState<{
    key: string;
    runs: RunSummary[] | null;
    error: string | null;
  }>({ key: "", runs: null, error: null });
  const loading = result.key !== key;
  const { runs, error } = result;

  useEffect(() => {
    const controller = new AbortController();
    client
      .runs({ compositionId: filter, limit: 50 }, controller.signal)
      .then((list) => setResult({ key, runs: list, error: null }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setResult((r) => ({
          key,
          runs: r.runs,
          error: describeApiError(err, "The saved runs could not be read."),
        }));
      });
    return () => controller.abort();
  }, [client, filter, key]);

  return (
    <section aria-labelledby="studio-saved-runs" aria-busy={loading}>
      <div className="flex items-center justify-between gap-2">
        <h3 id="studio-saved-runs" className={sectionTitle}>
          Your runs on the server{runs ? ` (${runs.length})` : ""}
        </h3>
        <button
          type="button"
          className={subtleButton}
          disabled={loading}
          onClick={() => setTick((n) => n + 1)}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>
      {compositionId ? (
        <label className="mt-1 flex items-center gap-1.5 text-[11px] text-slate-600 dark:text-slate-300">
          <input
            type="checkbox"
            className="accent-brand-600"
            checked={onlyThis}
            onChange={(e) => setOnlyThis(e.target.checked)}
          />
          Only runs of the composition on the canvas
        </label>
      ) : null}
      {error ? (
        <p
          role="alert"
          className="mt-1.5 rounded-lg bg-crit-bg px-2.5 py-1.5 text-[11px] text-crit-fg"
        >
          {error}
        </p>
      ) : null}
      {runs && !runs.length ? (
        <p
          className={`${panelCard} mt-1.5 border-dashed p-3 text-[11px] text-slate-500 dark:text-slate-400`}
        >
          No runs yet. Without a database the server keeps a run for an hour; with one, until its
          retention ends.
        </p>
      ) : null}
      {runs?.length ? (
        <ul className="mt-1.5 flex flex-col gap-1.5">
          {runs.map((r) => {
            const status: RunStatus = r.status;
            const opened = openedIds.has(r.run_id);
            return (
              <li key={r.run_id} className={`${panelCard} flex items-start gap-2 p-2.5`}>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate font-mono text-xs font-semibold text-slate-900 dark:text-slate-100">
                      {r.spec_name}
                    </span>
                    {r.composition_version ? (
                      <span className="font-mono text-[10px] text-slate-500 dark:text-slate-400">
                        v{r.composition_version}
                      </span>
                    ) : null}
                    <span
                      className={`rounded-full px-1.5 py-px text-[10px] font-semibold ${tonePill[runTone[status]]}`}
                    >
                      {runLabel[status]}
                    </span>
                  </div>
                  <p className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
                    <time dateTime={new Date(r.created_at * 1000).toISOString()}>
                      {ago(r.created_at)}
                    </time>
                    {r.pending_approvals?.length ? " · waiting for you" : ""}
                  </p>
                  <p className="font-mono text-[11px] text-slate-600 tabular-nums dark:text-slate-300">
                    {r.totals
                      ? `${formatCount(r.totals.tokens.total)} tok · ${formatMs(r.totals.wall_ms)}`
                      : `${formatCount(r.event_count)} events`}
                  </p>
                </div>
                <button
                  type="button"
                  className={subtleButton}
                  onClick={() => onOpen(r)}
                  disabled={locked && r.run_id !== liveRunId}
                  title={
                    locked && r.run_id !== liveRunId ? "Opens once the live run ends" : undefined
                  }
                  aria-label={`${opened ? "Show" : r.status === "running" ? "Follow" : "Replay"} run ${r.run_id.slice(0, 8)} of ${r.spec_name}`}
                >
                  {opened ? "Show" : r.status === "running" ? "Follow" : "Replay"}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
