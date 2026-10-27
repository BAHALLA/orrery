import { useEffect, useId, useRef } from "react";
import { CopyButton } from "../components/CopyButton";
import { AlertIcon, GaugeIcon, OrbitSpinner, StopIcon } from "../components/icons";
import { toneChip, tonePill, toneSolid, toneText } from "../components/severity";
import {
  budgetLabel,
  fieldLabel,
  formatBytes,
  formatCount,
  formatMs,
  input,
  invalidInput,
  nodeLabel,
  nodeTone,
  panelCard,
  primaryButton,
  runLabel,
  runTone,
  sectionTitle,
  subtleButton,
  toolbarButton,
} from "./format";
import type { StudioClient } from "./api";
import { ApprovalCards } from "./ApprovalCard";
import { PlayIcon } from "./icons";
import { budgetUse, isFinished, type RunView, waitingApprovals } from "./runState";
import type { StudioEvent } from "./types";
import type { Connection } from "./useStudioRun";
import type { Issue } from "./validate";

const MAX_INPUT = 8192;

interface Props {
  /** Reads and decides the run's pending approvals. */
  client: StudioClient;
  view: RunView | null;
  input: string;
  onInput: (text: string) => void;
  /** Focus the input whenever this changes (and is not 0). */
  focusSignal: number;
  inputIssues: Issue[];
  /** Problems the client found in the composition. */
  problems: number;
  busy: boolean;
  /** A cancel is on its way. */
  cancelling: boolean;
  connection: Connection;
  error: string | null;
  onClearError: () => void;
  onRun: () => void;
  onCancel: () => void;
  /** Follow a lost stream again. */
  onReconnect: () => void;
  now: number;
}

/** Start a run, watch it, and read it back: status, budgets, nodes, timeline, totals. */
export function RunPanel({
  client,
  view,
  input: text,
  onInput,
  focusSignal,
  inputIssues,
  problems,
  busy,
  cancelling,
  connection,
  error,
  onClearError,
  onRun,
  onCancel,
  onReconnect,
  now,
}: Props) {
  const inputId = useId();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (focusSignal) inputRef.current?.focus();
  }, [focusSignal]);
  const finished = view ? isFinished(view) : true;
  const tooLong = text.length > MAX_INPUT;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <label htmlFor={inputId} className={fieldLabel}>
          Run input
        </label>
        <textarea
          id={inputId}
          ref={inputRef}
          rows={3}
          value={text}
          onChange={(e) => onInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !busy && text.trim()) onRun();
          }}
          placeholder="What the first node receives, e.g. Kafka consumer lag"
          aria-invalid={inputIssues.length > 0 || tooLong}
          className={`${input} mt-1 resize-y ${inputIssues.length || tooLong ? invalidInput : ""}`}
        />
        <div className="mt-1 flex items-center gap-2 text-[11px] text-slate-500 dark:text-slate-400">
          <span className={tooLong ? "text-crit-fg" : ""}>
            {text.length} / {MAX_INPUT}
          </span>
          {inputIssues.map((i) => (
            <span key={i.msg} className="text-crit-fg">
              {i.msg}
            </span>
          ))}
          <span className="ml-auto hidden sm:inline">Ctrl+Enter runs</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {busy ? (
            <>
              <button
                type="button"
                className={`${toolbarButton} border border-crit-border text-crit-fg hover:bg-crit-bg`}
                onClick={onCancel}
                disabled={cancelling}
              >
                <StopIcon size={14} /> {cancelling ? "Cancelling…" : "Cancel run"}
              </button>
              {connection === "lost" ? (
                <button
                  type="button"
                  className={`${toolbarButton} border border-slate-300 dark:border-slate-700`}
                  onClick={onReconnect}
                >
                  Reconnect
                </button>
              ) : null}
            </>
          ) : (
            <button
              type="button"
              className={primaryButton}
              disabled={!text.trim() || tooLong}
              onClick={onRun}
            >
              <PlayIcon size={14} />
              {problems ? "Run anyway" : "Run"}
            </button>
          )}
          {problems && !busy ? (
            <span className="text-[11px] text-slate-500 dark:text-slate-400">
              {problems} problem{problems === 1 ? "" : "s"} found here; the server has the last
              word.
            </span>
          ) : null}
        </div>
      </div>

      {error ? (
        <div
          role="alert"
          className={`flex items-start gap-2 rounded-lg border px-2.5 py-2 text-xs ${toneChip.crit}`}
        >
          <AlertIcon size={14} className="mt-px shrink-0" />
          <span className="min-w-0 flex-1">{error}</span>
          <button type="button" className="font-medium underline" onClick={onClearError}>
            Dismiss
          </button>
        </div>
      ) : null}

      {view ? (
        <RunDetail
          client={client}
          view={view}
          connection={connection}
          finished={finished}
          now={now}
        />
      ) : (
        <EmptyRun />
      )}
    </div>
  );
}

function EmptyRun() {
  return (
    <div
      className={`${panelCard} border-dashed p-4 text-xs leading-relaxed text-slate-500 dark:text-slate-400`}
    >
      <p className="font-medium text-slate-700 dark:text-slate-200">Nothing has run yet.</p>
      <p className="mt-1">
        A run is separate from the live system: it never changes agents, the catalogue, chat
        sessions or memory, and it is held to autonomy L2 (reads only). Its trace streams here as it
        happens, and the canvas lights up node by node.
      </p>
    </div>
  );
}

function RunDetail({
  client,
  view,
  connection,
  finished,
  now,
}: {
  client: StudioClient;
  view: RunView;
  connection: Connection;
  finished: boolean;
  now: number;
}) {
  const tone = runTone[view.status];
  const elapsed =
    view.totals?.wall_ms ??
    (view.startedAt
      ? Math.max(0, ((finished ? (view.endedAt ?? now) : now) - view.startedAt) * 1000)
      : null);
  const budgets = budgetUse(view, now);

  return (
    <>
      <div className={`${panelCard} p-3`}>
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-semibold ${tonePill[tone]}`}
          >
            {finished ? (
              <span className={`h-1.5 w-1.5 rounded-full ${toneSolid[tone]}`} aria-hidden="true" />
            ) : (
              <OrbitSpinner size={12} />
            )}
            {runLabel[view.status]}
          </span>
          <span className="font-mono text-xs text-slate-600 tabular-nums dark:text-slate-300">
            {formatMs(elapsed)}
          </span>
          {connection === "reconnecting" ? (
            <span className={`ml-auto text-[11px] font-medium ${toneText.warn}`}>
              reconnecting…
            </span>
          ) : connection === "lost" ? (
            <span className={`ml-auto text-[11px] font-medium ${toneText.crit}`}>stream lost</span>
          ) : null}
        </div>
        <p className="mt-1.5 flex items-center gap-1 font-mono text-[11px] text-slate-500 dark:text-slate-400">
          <span className="truncate">
            {view.specName ?? "…"} · run {view.runId.slice(0, 8)}
          </span>
          <CopyButton value={view.runId} label="Copy run id" />
        </p>
        {view.error && view.status !== "succeeded" ? (
          <p className="mt-1.5 text-xs text-crit-fg">{view.error}</p>
        ) : null}
      </div>

      <ApprovalCards client={client} runId={view.runId} asks={waitingApprovals(view)} now={now} />

      {view.budgetHit ? (
        <div
          role="alert"
          className={`flex items-start gap-2 rounded-lg border-2 px-3 py-2.5 text-sm ${toneChip.warn}`}
        >
          <GaugeIcon size={18} className="mt-px shrink-0" />
          <div>
            <p className="font-semibold">
              Budget hit: {budgetLabel[view.budgetHit.budget] ?? view.budgetHit.budget}
            </p>
            <p className="text-xs">
              {formatCount(view.budgetHit.used)} used of {formatCount(view.budgetHit.limit)}. The
              run stops here and ends as “budget exhausted”; a tool call past a budget is refused
              before it runs.
            </p>
          </div>
        </div>
      ) : null}

      {budgets.length ? (
        <section aria-labelledby="studio-budgets">
          <h3 id="studio-budgets" className={sectionTitle}>
            Budgets
          </h3>
          <ul className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-2">
            {budgets.map((b) => {
              const share = b.limit ? Math.min(1, b.used / b.limit) : 0;
              const hit = view.budgetHit?.budget === b.name;
              const barTone = hit ? "crit" : share > 0.8 ? "warn" : "running";
              return (
                <li key={b.name}>
                  <div className="flex items-baseline justify-between text-[11px]">
                    <span className="text-slate-600 dark:text-slate-300">
                      {budgetLabel[b.name] ?? b.name}
                    </span>
                    <span className="font-mono text-slate-500 tabular-nums dark:text-slate-400">
                      {formatCount(b.used)}/{formatCount(b.limit)}
                    </span>
                  </div>
                  <div
                    role="meter"
                    aria-label={`${budgetLabel[b.name] ?? b.name} used`}
                    aria-valuemin={0}
                    aria-valuemax={b.limit}
                    aria-valuenow={Math.min(b.used, b.limit)}
                    className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800"
                  >
                    <div
                      className={`h-full rounded-full transition-[width] duration-500 ${toneSolid[barTone]}`}
                      style={{ width: `${Math.round(share * 100)}%` }}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      {view.order.length ? (
        <section aria-labelledby="studio-node-runs">
          <h3 id="studio-node-runs" className={sectionTitle}>
            Nodes in this run
          </h3>
          <ol className="mt-1.5 flex flex-col">
            {view.order.map((id, i) => {
              const n = view.nodes[id];
              if (!n) return null;
              const t = nodeTone[n.status];
              const live = n.status === "active" && n.enteredAt ? (now - n.enteredAt) * 1000 : null;
              return (
                <li key={id} className="flex items-center gap-2 py-1 text-xs">
                  <span className="w-4 text-right font-mono text-[10px] text-slate-500 dark:text-slate-400">
                    {i + 1}
                  </span>
                  <span
                    className={`h-2 w-2 shrink-0 rounded-full ${toneSolid[t]} ${n.status === "active" ? "motion-safe:animate-pulse" : ""}`}
                    aria-hidden="true"
                  />
                  <span className="min-w-0 flex-1 truncate font-mono text-slate-800 dark:text-slate-200">
                    {id}
                  </span>
                  <span className={`text-[10px] font-semibold uppercase ${toneText[t]}`}>
                    {nodeLabel[n.status]}
                  </span>
                  <span className="w-16 text-right font-mono text-slate-500 tabular-nums dark:text-slate-400">
                    {formatCount(n.tokens.total)} tok
                  </span>
                  <span className="w-12 text-right font-mono text-slate-500 tabular-nums dark:text-slate-400">
                    {formatMs(live ?? n.elapsedMs)}
                  </span>
                </li>
              );
            })}
          </ol>
        </section>
      ) : null}

      {view.totals ? <TotalsCard view={view} /> : null}

      {view.input || view.output ? (
        <section aria-labelledby="studio-content" className="flex flex-col gap-2">
          <h3 id="studio-content" className={sectionTitle}>
            Content (redacted)
          </h3>
          {view.input ? <ContentBlock label="Input" text={view.input} /> : null}
          {view.output ? <ContentBlock label="Output" text={view.output} /> : null}
        </section>
      ) : null}

      <Timeline view={view} />
    </>
  );
}

function ContentBlock({ label, text }: { label: string; text: string }) {
  return (
    <figure className={`${panelCard} p-2.5`}>
      <figcaption className="flex items-center text-[11px] font-medium text-slate-500 dark:text-slate-400">
        {label}
        <CopyButton value={text} label={`Copy ${label.toLowerCase()}`} className="ml-auto" />
      </figcaption>
      <pre className="orrery-scroll mt-1 max-h-48 overflow-auto font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-slate-800 dark:text-slate-200">
        {text}
      </pre>
    </figure>
  );
}

function TotalsCard({ view }: { view: RunView }) {
  const t = view.totals;
  if (!t) return null;
  const tiles: [string, string, (string | undefined)?][] = [
    [
      "Tokens",
      formatCount(t.tokens.total),
      `${formatCount(t.tokens.input)} in · ${formatCount(t.tokens.output)} out`,
    ],
    ["Wall time", formatMs(t.wall_ms)],
    ["Tool calls", String(t.tool_calls)],
    ["Remote calls", String(t.remote_calls)],
    ["Delegations", String(t.delegations)],
    [
      "Events",
      String(view.events.length),
      view.droppedEvents ? `${view.droppedEvents} dropped` : undefined,
    ],
  ];
  return (
    <section aria-labelledby="studio-totals">
      <h3 id="studio-totals" className={sectionTitle}>
        Totals
      </h3>
      <dl className="mt-1.5 grid grid-cols-3 gap-1.5">
        {tiles.map(([label, value, sub]) => (
          <div key={label} className={`${panelCard} px-2.5 py-2`}>
            <dt className="text-[10px] font-medium tracking-wide text-slate-500 uppercase dark:text-slate-400">
              {label}
            </dt>
            <dd className="font-mono text-base font-semibold text-slate-900 tabular-nums dark:text-slate-100">
              {value}
            </dd>
            {sub ? <dd className="text-[10px] text-slate-500 dark:text-slate-400">{sub}</dd> : null}
          </div>
        ))}
      </dl>
      {Object.keys(t.tokens_by_model).length ? (
        <ul className="mt-1.5 flex flex-col gap-0.5 text-[11px] text-slate-600 dark:text-slate-300">
          {Object.entries(t.tokens_by_model).map(([model, count]) => (
            <li key={model} className="flex justify-between font-mono">
              <span className="truncate">{model}</span>
              <span className="tabular-nums">{formatCount(count.total)} tok</span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

const eventTone: Record<string, string> = {
  "run.start": tonePill.running,
  "run.end": tonePill.neutral,
  "node.enter": tonePill.running,
  "node.exit": tonePill.ok,
  "mcp.call": "bg-sky-50 text-sky-800 dark:bg-sky-950/60 dark:text-sky-200",
  "a2a.message": "bg-violet-50 text-violet-800 dark:bg-violet-950/60 dark:text-violet-200",
  "budget.hit": tonePill.warn,
  "approval.requested": tonePill.warn,
  "approval.decided": tonePill.neutral,
};

/** One line per event: what happened, in plain words. */
function describeEvent(e: StudioEvent): string {
  switch (e.type) {
    case "run.start":
      return `${e.spec_name} started: ${e.nodes.join(" → ")}`;
    case "node.enter":
      return `${e.node_id} started on ${e.model}`;
    case "node.exit":
      return `${e.node_id} ${e.outcome === "ok" ? "finished" : e.outcome.replace("_", " ")} · ${formatCount(e.tokens.total)} tok · ${formatMs(e.elapsed_ms)}`;
    case "mcp.call":
      return `${e.node_id} called ${e.entry}.${e.tool} · ${e.outcome}${e.elapsed_ms != null ? ` · ${formatMs(e.elapsed_ms)}` : ""}`;
    case "a2a.message":
      return e.direction === "outbound"
        ? `${e.node_id} → ${e.remote}.${e.skill} ${e.method}${e.parts?.length ? ` · ${e.parts.map((p) => `${p.kind} ${formatBytes(p.size)}`).join(", ")}` : ""}`
        : `${e.remote} → ${e.node_id}: ${e.task_state ?? e.status ?? "reply"}${e.elapsed_ms != null ? ` · ${formatMs(e.elapsed_ms)}` : ""}`;
    case "approval.requested":
      return e.kind === "input"
        ? `${e.remote ?? "a remote agent"} asked you a question (${e.node_id})`
        : `${e.node_id} is waiting for a person: ${e.prompt}`;
    case "approval.decided":
      return `${e.node_id} ${e.decision}${e.waited_ms != null ? ` after ${formatMs(e.waited_ms)}` : ""}`;
    case "budget.hit":
      return `budget ${e.budget} hit: ${e.used} of ${e.limit}`;
    case "run.end":
      return `run ${e.outcome.replace("_", " ")}${e.error ? `: ${e.error}` : ""}`;
    default:
      return (e as { type: string }).type;
  }
}

function Timeline({ view }: { view: RunView }) {
  const t0 = view.startedAt ?? 0;
  return (
    <section aria-labelledby="studio-timeline">
      <div className="flex items-center">
        <h3 id="studio-timeline" className={sectionTitle}>
          Timeline
        </h3>
        <span className="ml-auto text-[11px] text-slate-500 dark:text-slate-400">
          {view.events.length} events
        </span>
      </div>
      {view.events.length === 0 ? (
        <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
          Waiting for the first event…
        </p>
      ) : (
        <ol className="relative mt-1.5 flex flex-col border-l border-slate-200 pl-3 dark:border-slate-800">
          {view.events.map((e) => (
            <li key={e.seq} className="relative py-1">
              <span
                aria-hidden="true"
                className={`absolute top-2.5 -left-[16.5px] h-2 w-2 rounded-full ring-2 ring-white dark:ring-slate-900 ${
                  e.type === "budget.hit"
                    ? toneSolid.warn
                    : e.type === "run.end"
                      ? toneSolid[runTone[view.status]]
                      : "bg-slate-300 dark:bg-slate-600"
                }`}
              />
              <div className="flex items-baseline gap-2">
                <span className="w-12 shrink-0 font-mono text-[10px] text-slate-500 tabular-nums dark:text-slate-400">
                  +{(e.ts - t0 || 0).toFixed(1)}s
                </span>
                <span
                  className={`shrink-0 rounded px-1 py-px font-mono text-[10px] ${eventTone[e.type] ?? tonePill.neutral}`}
                >
                  {e.type}
                </span>
              </div>
              <p className="pl-14 text-xs leading-snug text-slate-700 dark:text-slate-300">
                {describeEvent(e)}
              </p>
            </li>
          ))}
        </ol>
      )}
      {view.events.length ? (
        <p className="mt-2">
          <CopyButton
            value={JSON.stringify(view.events, null, 2)}
            label="Copy events as JSON"
            className={subtleButton}
          />
        </p>
      ) : null}
    </section>
  );
}
