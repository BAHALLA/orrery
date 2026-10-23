import { memo } from "react";
import type { Trace, TraceCall } from "../chat/trace";
import { agentLabel, flatten, refusalReason, summarize } from "../chat/trace";
import { useShowApproval } from "./approval";
import { AlertIcon, CheckIcon, ChevronIcon, LockIcon, OrbitSpinner, ShieldIcon } from "./icons";
import { toneChip, tonePill, toneText } from "./severity";

interface Props {
  trace: Trace;
  /** True while the turn is still running: the trace stays open and live. */
  live: boolean;
  durationMs?: number | undefined;
}

const stateStyle: Record<TraceCall["state"], string> = {
  running: toneText.running,
  ok: toneText.ok,
  error: toneText.crit,
  awaiting: toneText.warn,
  refused: toneText.refused,
};

const stateLabel: Record<TraceCall["state"], string> = {
  running: "running",
  ok: "done",
  error: "failed",
  awaiting: "awaiting your approval",
  refused: "refused",
};

function StateIcon({ state }: { state: TraceCall["state"] }) {
  const cls = stateStyle[state];
  if (state === "running") return <OrbitSpinner className={cls} />;
  if (state === "ok") return <CheckIcon size={14} className={cls} />;
  if (state === "awaiting") return <ShieldIcon size={14} className={cls} />;
  if (state === "refused") return <LockIcon size={14} className={cls} />;
  return <AlertIcon size={14} className={cls} />;
}

function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** The chip's plain-language state, for the tooltip and screen readers. */
function describe(call: TraceCall): string {
  if (call.state === "refused") return `refused: ${refusalReason(call.status)}`;
  return stateLabel[call.state];
}

const chipBase = "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs";

function ToolChip({ call }: { call: TraceCall }) {
  const showApproval = useShowApproval();

  // Paused for approval: amber, and a shortcut to the card that decides it.
  if (call.state === "awaiting") {
    const body = (
      <>
        <StateIcon state="awaiting" />
        <span className="font-mono">{call.tool}</span>
        <span className="font-medium">awaiting approval</span>
      </>
    );
    return (
      <li className="inline-flex">
        {showApproval ? (
          <button
            type="button"
            onClick={showApproval}
            title={`${call.tool} is waiting for your approval — review it`}
            className={`${chipBase} ${toneChip.warn} cursor-pointer hover:brightness-95 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none`}
          >
            {body}
            <span aria-hidden="true">↓</span>
            <span className="sr-only">: review it</span>
          </button>
        ) : (
          <span
            className={`${chipBase} ${toneChip.warn}`}
            title={`${call.tool}: paused for approval`}
          >
            {body}
          </span>
        )}
      </li>
    );
  }

  // A gate said no. Neutral and explained, never red: at autonomy L2 this is
  // the platform answering a write exactly as configured.
  if (call.state === "refused") {
    const reason = refusalReason(call.status);
    return (
      <li
        className={`${chipBase} border-dashed ${toneChip.refused}`}
        title={`${call.tool} was refused: ${reason}${call.status ? ` (${call.status})` : ""}`}
      >
        <StateIcon state="refused" />
        <span className="font-mono">{call.tool}</span>
        <span>
          <span className="sr-only">refused: </span>
          <span aria-hidden="true">· </span>
          {reason}
        </span>
      </li>
    );
  }

  return (
    <li
      className={`${chipBase} border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900`}
      title={`${call.tool}: ${describe(call)}${call.status ? ` (${call.status})` : ""}`}
    >
      <StateIcon state={call.state} />
      <span className="font-mono text-slate-700 dark:text-slate-200">{call.tool}</span>
      {call.guarded ? (
        <span className={`rounded px-1 text-[10px] font-semibold uppercase ${tonePill.guarded}`}>
          guarded
        </span>
      ) : null}
      {call.elapsedMs != null ? (
        <span className="text-slate-500 tabular-nums dark:text-slate-400">
          {formatMs(call.elapsedMs)}
        </span>
      ) : null}
      <span className="sr-only">{describe(call)}</span>
    </li>
  );
}

function Delegation({ call }: { call: TraceCall }) {
  const tools = call.children.filter((c) => !c.delegation);
  const nested = call.children.filter((c) => c.delegation);
  return (
    <li className="rounded-lg border border-slate-200 bg-slate-50/70 p-2.5 dark:border-slate-800 dark:bg-slate-900/60">
      <div className="flex items-center gap-2 text-sm">
        <StateIcon state={call.state} />
        <span className="font-medium text-slate-800 dark:text-slate-100">
          {agentLabel(call.tool)}
        </span>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          {call.state === "running" ? "working…" : describe(call)}
        </span>
        {call.elapsedMs != null ? (
          <span className="ml-auto text-xs text-slate-500 tabular-nums dark:text-slate-400">
            {formatMs(call.elapsedMs)}
          </span>
        ) : null}
      </div>
      {tools.length > 0 ? (
        <ul
          className="mt-2 flex flex-wrap gap-1.5 pl-6"
          aria-label={`${agentLabel(call.tool)} tool calls`}
        >
          {tools.map((t) => (
            <ToolChip key={t.callId} call={t} />
          ))}
        </ul>
      ) : null}
      {nested.length > 0 ? (
        <ul className="mt-2 flex flex-col gap-2 pl-6">
          {nested.map((n) => (
            <Delegation key={n.callId} call={n} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function TraceBody({ trace }: { trace: Trace }) {
  const delegations = trace.filter((c) => c.delegation);
  const direct = trace.filter((c) => !c.delegation);
  return (
    <div className="flex flex-col gap-2">
      {delegations.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {delegations.map((d) => (
            <Delegation key={d.callId} call={d} />
          ))}
        </ul>
      ) : null}
      {direct.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5" aria-label="Tool calls">
          {direct.map((t) => (
            <ToolChip key={t.callId} call={t} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * What the agent did for this answer, as it happened.
 *
 * Live, it is open and updating — the specialists being consulted and each
 * one's tool calls, so a minute-long sweep reads as progress instead of a
 * spinner. Finished, it folds into a one-line summary that expands on demand:
 * the answer is what the reader came for, the trace is the receipt.
 */
export const RunTrace = memo(function RunTrace({ trace, live, durationMs }: Props) {
  if (live) {
    const running = flatten(trace).filter((c) => c.state === "running");
    const current = running.at(-1);
    return (
      <section
        aria-label="Agent progress"
        aria-live="polite"
        className="mb-3 rounded-xl border border-brand-200/70 bg-brand-50/50 p-3 dark:border-brand-900/60 dark:bg-brand-950/30"
      >
        <div className="mb-2 flex items-center gap-2 text-sm text-slate-700 dark:text-slate-200">
          <OrbitSpinner size={16} className="text-brand-500" />
          <span className="font-medium">
            {current
              ? current.delegation
                ? `Asking ${agentLabel(current.tool)}…`
                : `Running ${current.tool}…`
              : "Thinking…"}
          </span>
        </div>
        {trace.length > 0 ? <TraceBody trace={trace} /> : null}
      </section>
    );
  }

  if (trace.length === 0) return null;
  return (
    <details className="group mb-3 rounded-xl border border-slate-200 bg-slate-50/60 dark:border-slate-800 dark:bg-slate-900/40">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-xl px-3 py-2 text-xs text-slate-500 select-none hover:text-slate-800 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:text-slate-400 dark:hover:text-slate-200">
        <ChevronIcon size={14} className="transition group-open:rotate-90" />
        <span>{summarize(trace)}</span>
        {durationMs != null ? (
          <span className="ml-auto tabular-nums">{formatMs(durationMs)}</span>
        ) : null}
      </summary>
      <div className="px-3 pb-3">
        <TraceBody trace={trace} />
      </div>
    </details>
  );
});
