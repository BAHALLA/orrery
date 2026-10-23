import type { Ref } from "react";
import type { PendingConfirmation } from "../api/types";
import { AlertIcon, CheckIcon, CloseIcon, ShieldIcon } from "./icons";
import { toneBorder, tonePill } from "./severity";

interface Props {
  /** The card itself, so the transcript's "awaiting approval" chips can bring
   * it into view and focus it. */
  panelRef?: Ref<HTMLElement>;
  pending: PendingConfirmation;
  disabled: boolean;
  onDecide: (word: "approve" | "deny") => void;
}

const LEVELS: Record<string, { label: string; explain: string; tone: string; badge: string }> = {
  destructive: {
    label: "Destructive",
    explain: "This removes or restarts something and may not be reversible.",
    tone: toneBorder.crit,
    badge: tonePill.crit,
  },
  confirm: {
    label: "Changes state",
    explain: "This changes a live system. Check the target before approving.",
    tone: toneBorder.warn,
    badge: tonePill.warn,
  },
};

function ago(createdAt: number): string {
  if (!createdAt) return "";
  const secs = Math.max(0, Math.round(Date.now() / 1000 - createdAt));
  if (secs < 60) return `requested ${secs}s ago`;
  return `requested ${Math.round(secs / 60)}m ago`;
}

/**
 * Approve/Deny card for the caller's pending guarded action.
 *
 * Strictly a renderer: the buttons send the literal words "approve"/"deny"
 * through the normal chat flow, and the server's requester-verified gate
 * remains the sole authority on whether that decision authorizes anything.
 * The moment this component started deciding *who* may approve, the
 * guarantee would be bypassed — so it never does.
 */
export function ConfirmationPanel({ panelRef, pending, disabled, onDecide }: Props) {
  const argEntries = Object.entries(pending.args);
  const level = LEVELS[pending.level] ?? LEVELS.confirm;
  const Icon = pending.level === "destructive" ? AlertIcon : ShieldIcon;

  return (
    <div className="px-3 sm:px-6">
      <section
        ref={panelRef}
        tabIndex={-1}
        role="alertdialog"
        aria-label="Pending confirmation"
        aria-describedby="confirmation-explain"
        className={`mx-auto mb-3 w-full max-w-3xl overflow-hidden rounded-2xl border-2 bg-white shadow-lg shadow-slate-900/5 dark:bg-slate-900 ${level.tone}`}
      >
        <div className="flex items-start gap-3 p-4">
          <span className={`rounded-lg p-2 ${level.badge}`}>
            <Icon size={18} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                Awaiting your approval
              </span>
              <span
                className={`rounded-full px-2 py-0.5 text-[11px] font-semibold tracking-wide uppercase ${level.badge}`}
              >
                {pending.level ? level.label : "guarded"}
              </span>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {ago(pending.created_at)}
              </span>
            </div>
            <p className="mt-1 font-mono text-sm font-medium text-slate-800 dark:text-slate-200">
              {pending.tool_name}
            </p>
            <p
              id="confirmation-explain"
              className="mt-1 text-xs text-slate-500 dark:text-slate-400"
            >
              {level.explain} Only you can approve it — the action runs as you.
            </p>
          </div>
        </div>

        {argEntries.length > 0 ? (
          <dl className="grid gap-x-4 gap-y-1 border-t border-slate-100 bg-slate-50/70 px-4 py-3 text-sm sm:grid-cols-[auto_1fr] dark:border-slate-800 dark:bg-slate-950/40">
            {argEntries.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="text-slate-500 dark:text-slate-400">{key}</dt>
                <dd className="m-0 font-mono break-all text-slate-800 dark:text-slate-200">
                  {typeof value === "string" ? value : JSON.stringify(value)}
                </dd>
              </div>
            ))}
          </dl>
        ) : null}

        <div className="flex justify-end gap-2 border-t border-slate-100 px-4 py-3 dark:border-slate-800">
          <button
            type="button"
            disabled={disabled}
            onClick={() => onDecide("deny")}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none disabled:opacity-50 dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800"
          >
            <CloseIcon size={16} />
            Deny
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onDecide("approve")}
            className="inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-500 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:outline-none disabled:opacity-50 dark:focus-visible:ring-offset-slate-900"
          >
            <CheckIcon size={16} />
            Approve
          </button>
        </div>
      </section>
    </div>
  );
}
