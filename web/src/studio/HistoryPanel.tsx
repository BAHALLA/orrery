import { useState } from "react";
import { tonePill } from "../components/severity";
import {
  formatCount,
  formatMs,
  panelCard,
  runLabel,
  runTone,
  sectionTitle,
  subtleButton,
} from "./format";
import { compareRuns, type CompareRow, type RunRecord } from "./history";
import { tokensSoFar } from "./runState";

function value(row: CompareRow, v: number | null): string {
  if (v === null) return "—";
  return row.unit === "ms" ? formatMs(v) : formatCount(v);
}

function delta(row: CompareRow): { text: string; tone: string } {
  if (row.delta === null || row.delta === 0)
    return { text: row.delta === 0 ? "=" : "", tone: "text-slate-500 dark:text-slate-400" };
  const sign = row.delta > 0 ? "+" : "−";
  const abs = Math.abs(row.delta);
  return {
    text: `${sign}${row.unit === "ms" ? formatMs(abs) : formatCount(abs)}`,
    // More of anything (tokens, time, calls) costs more: red up, green down.
    tone: row.delta > 0 ? "text-crit-fg" : "text-ok-fg",
  };
}

/**
 * This session's runs, newest first. Pick one to show it again on the canvas
 * and in the Run tab, or two to compare their totals and per-node cost.
 */
export function HistoryPanel({
  records,
  shownId,
  onShow,
}: {
  records: RunRecord[];
  shownId: string | null;
  onShow: (runId: string) => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const toggle = (id: string) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id].slice(-2)));
  const [a, b] = picked
    .map((id) => records.find((r) => r.runId === id))
    .filter((r): r is RunRecord => !!r)
    // A is the earlier run, B the later, whichever was ticked first: B − A then
    // reads as "what changed since".
    .sort((x, y) => x.startedAt - y.startedAt);
  const compared = a && b ? compareRuns(a.view, b.view) : null;

  if (!records.length) {
    return (
      <p className={`${panelCard} border-dashed p-4 text-xs text-slate-500 dark:text-slate-400`}>
        Runs you start in this session are kept here, so two of them can be compared side by side.
        They are not saved yet: stored runs and replay come with P0b.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <section aria-labelledby="studio-history">
        <h3 id="studio-history" className={sectionTitle}>
          This session ({records.length})
        </h3>
        <p className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
          Tick two runs to compare them.
        </p>
        <ul className="mt-1.5 flex flex-col gap-1.5">
          {records.map((r) => {
            const tone = runTone[r.view.status];
            const on = picked.includes(r.runId);
            return (
              <li
                key={r.runId}
                className={`${panelCard} flex items-start gap-2 p-2.5 ${r.runId === shownId ? "border-brand-300 dark:border-brand-800" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => toggle(r.runId)}
                  aria-label={`Compare run ${r.runId.slice(0, 8)} of ${r.spec.name}`}
                  className="mt-1 accent-brand-600"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate font-mono text-xs font-semibold text-slate-900 dark:text-slate-100">
                      {r.spec.name}
                    </span>
                    <span
                      className={`rounded-full px-1.5 py-px text-[10px] font-semibold ${tonePill[tone]}`}
                    >
                      {runLabel[r.view.status]}
                    </span>
                  </div>
                  <p
                    className="mt-0.5 truncate text-[11px] text-slate-500 dark:text-slate-400"
                    title={r.input}
                  >
                    {new Date(r.startedAt).toLocaleTimeString()} · {r.spec.nodes.length} nodes · “
                    {r.input}”
                  </p>
                  <p className="font-mono text-[11px] text-slate-600 tabular-nums dark:text-slate-300">
                    {formatCount(tokensSoFar(r.view).total)} tok ·{" "}
                    {formatMs(r.view.totals?.wall_ms ?? null)}
                  </p>
                </div>
                <button
                  type="button"
                  className={subtleButton}
                  aria-current={r.runId === shownId ? "true" : undefined}
                  onClick={() => onShow(r.runId)}
                >
                  {r.runId === shownId ? "Shown" : "Show"}
                </button>
              </li>
            );
          })}
        </ul>
      </section>

      {a && b && compared ? (
        <section aria-labelledby="studio-compare">
          <h3 id="studio-compare" className={sectionTitle}>
            Compare
          </h3>
          <table className="mt-1.5 w-full text-xs">
            <caption className="sr-only">
              Run {a.runId.slice(0, 8)} (A) against run {b.runId.slice(0, 8)} (B)
            </caption>
            <thead className="text-[10px] tracking-wide text-slate-500 uppercase dark:text-slate-400">
              <tr>
                <th scope="col" className="py-1 text-left font-medium">
                  &nbsp;
                </th>
                <th scope="col" className="py-1 pl-3 text-right align-bottom font-medium">
                  A
                  <span className="block font-mono tracking-normal normal-case">
                    {a.runId.slice(0, 8)}
                  </span>
                </th>
                <th scope="col" className="py-1 pl-3 text-right align-bottom font-medium">
                  B
                  <span className="block font-mono tracking-normal normal-case">
                    {b.runId.slice(0, 8)}
                  </span>
                </th>
                <th scope="col" className="py-1 pl-3 text-right align-bottom font-medium">
                  B − A
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 font-mono tabular-nums dark:divide-slate-800">
              {[...compared.totals, ...compared.nodes].map((row, i) => {
                const d = delta(row);
                return (
                  <tr
                    key={row.label}
                    className={
                      i === compared.totals.length
                        ? "border-t-2 border-slate-200 dark:border-slate-700"
                        : ""
                    }
                  >
                    <th
                      scope="row"
                      className="py-1 pr-2 text-left font-sans font-normal text-slate-600 dark:text-slate-300"
                    >
                      {row.label}
                    </th>
                    <td className="py-1 pl-3 text-right text-slate-800 dark:text-slate-200">
                      {value(row, row.a)}
                    </td>
                    <td className="py-1 pl-3 text-right text-slate-800 dark:text-slate-200">
                      {value(row, row.b)}
                    </td>
                    <td className={`py-1 pl-3 text-right ${d.tone}`}>{d.text}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  );
}
