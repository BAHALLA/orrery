import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { AlertIcon, CheckIcon, OrbitSpinner } from "../components/icons";
import { toneChip, toneText } from "../components/severity";
import { formatCount, formatMs, nodeLabel, nodeTone, toolTone } from "./format";
import type { StudioNode } from "./graph";
import { KindIcon } from "./icons";
import { KINDS, parseToolRef, toolLabel } from "./kinds";
import type { NodeRun } from "./runState";
import type { Issue } from "./validate";

/** What the canvas adds to a node's spec for drawing it; never part of the spec. */
export interface NodeDecor {
  issues: Issue[];
  run: NodeRun | null;
  /** "coming in P0b" for a kind this server cannot run. */
  needs: string | null;
  /** Unix seconds, ticking while the node runs. */
  now: number;
  /** The graph flows top to bottom: handles go on the top and bottom edges. */
  vertical: boolean;
}

const ring: Record<string, string> = {
  active: "border-running-solid ring-4 ring-running-solid/25 shadow-lg shadow-running-solid/20",
  ok: "border-ok-border",
  failed: "border-crit-solid ring-2 ring-crit-solid/20",
  cancelled: "border-refused-border border-dashed",
  budget_exhausted: "border-warn-solid ring-2 ring-warn-solid/25",
};

// React Flow styles handles itself; `!` wins over its stylesheet.
const handle =
  "h-2.5! w-2.5! border-2! border-white! bg-slate-400! dark:border-slate-900! dark:bg-slate-500!";

/** One node on the canvas: kind, id, what it does, its tools, and its live run state. */
export const NodeCard = memo(function NodeCard({ data, selected }: NodeProps<StudioNode>) {
  const decor = data as StudioNode["data"] & Partial<NodeDecor>;
  const { spec } = decor;
  const issues = decor.issues ?? [];
  const run = decor.run ?? null;
  const status = run?.status ?? "pending";
  const kind = KINDS[spec.kind];
  const tools = spec.tools ?? [];
  const summary = spec.description || spec.instruction || spec.prompt || kind.blurb;
  const live =
    status === "active" && run?.enteredAt && decor.now
      ? Math.max(0, (decor.now - run.enteredAt) * 1000)
      : null;

  return (
    <div
      className={`group relative w-60 rounded-xl border bg-white text-left shadow-sm transition-[box-shadow,border-color] duration-300 dark:bg-slate-900 ${
        ring[status] ?? "border-slate-200 dark:border-slate-700"
      } ${selected ? "outline-2 outline-offset-2 outline-brand-500" : ""} ${
        decor.needs ? "border-dashed opacity-80" : ""
      } ${issues.length && status === "pending" ? "border-crit-border" : ""}`}
    >
      <Handle
        type="target"
        position={decor.vertical ? Position.Top : Position.Left}
        className={handle}
      />
      {status === "active" ? (
        <span
          aria-hidden="true"
          className="studio-halo pointer-events-none absolute -inset-1.5 rounded-2xl border-2 border-running-solid"
        />
      ) : null}
      <div className="flex items-center gap-2 px-3 pt-2.5">
        <span
          className={`grid h-6 w-6 shrink-0 place-items-center rounded-md ${
            status === "active"
              ? "bg-running-solid text-white"
              : "bg-brand-50 text-brand-700 dark:bg-brand-950/70 dark:text-brand-200"
          }`}
        >
          <KindIcon kind={spec.kind} size={14} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate font-mono text-[13px] font-semibold text-slate-900 dark:text-slate-100">
            {spec.id}
          </span>
          <span className="block text-[10px] font-medium tracking-wide text-slate-500 uppercase dark:text-slate-400">
            {kind.label}
          </span>
        </span>
        {issues.length ? (
          <span
            className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold ${toneChip.crit}`}
            title={issues.map((i) => i.msg).join("\n")}
          >
            <AlertIcon size={11} />
            {issues.length}
          </span>
        ) : status === "active" ? (
          <OrbitSpinner className={toneText.running} />
        ) : status === "ok" ? (
          <CheckIcon size={14} className={toneText.ok} />
        ) : status !== "pending" ? (
          <AlertIcon size={14} className={toneText[nodeTone[status]]} />
        ) : null}
      </div>

      <p className="line-clamp-2 px-3 pt-1 text-xs leading-snug text-slate-600 dark:text-slate-400">
        {summary || <span className="italic">No instruction yet</span>}
      </p>

      {decor.needs ? (
        <p className="px-3 pt-1.5">
          <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${toneChip.refused}`}>
            {decor.needs}
          </span>
        </p>
      ) : null}

      {tools.length ? (
        <ul className="flex flex-wrap gap-1 px-3 pt-2" aria-label="Tools">
          {tools.slice(0, 3).map((ref) => {
            const tone = toolTone[parseToolRef(ref)?.kind ?? "catalog"];
            return (
              <li
                key={ref}
                className={`max-w-full truncate rounded px-1.5 py-0.5 font-mono text-[10px] ring-1 ring-inset ${tone}`}
              >
                {toolLabel(ref)}
              </li>
            );
          })}
          {tools.length > 3 ? (
            <li className="rounded px-1 py-0.5 text-[10px] text-slate-500 dark:text-slate-400">
              +{tools.length - 3}
            </li>
          ) : null}
        </ul>
      ) : null}

      <div
        className={`mt-2.5 flex items-center gap-2 border-t px-3 py-1.5 font-mono text-[11px] tabular-nums ${
          run && status !== "pending"
            ? "border-slate-100 text-slate-600 dark:border-slate-800 dark:text-slate-300"
            : "border-transparent text-slate-500 dark:text-slate-400"
        }`}
      >
        {run && status !== "pending" ? (
          <>
            <span
              className={`font-sans text-[10px] font-semibold uppercase ${toneText[nodeTone[status]]}`}
            >
              {nodeLabel[status]}
            </span>
            <span className="ml-auto">{formatCount(run.tokens.total)} tok</span>
            <span aria-hidden="true">·</span>
            <span>{formatMs(live ?? run.elapsedMs)}</span>
          </>
        ) : (
          <span className="font-sans text-[10px]">
            {run ? "waiting" : spec.kind === "llm_agent" ? "deployment model" : ""}
          </span>
        )}
      </div>
      <Handle
        type="source"
        position={decor.vertical ? Position.Bottom : Position.Right}
        className={handle}
      />
    </div>
  );
});
