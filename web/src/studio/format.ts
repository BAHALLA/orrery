import type { StatusTone } from "../components/severity";
import type { NodeRunStatus, RunStatus } from "./runState";

/** Shared styling and number formatting for the Studio's panels. */

export const sectionTitle =
  "text-[11px] font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400";

export const fieldLabel = "block text-xs font-medium text-slate-700 dark:text-slate-300";

export const input =
  "w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 placeholder:text-slate-400 focus:border-brand-500 focus:ring-2 focus:ring-brand-500/30 focus:outline-none disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-500 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100 dark:placeholder:text-slate-500 dark:disabled:bg-slate-900 dark:disabled:text-slate-400";

export const invalidInput = "border-crit-border focus:border-crit-solid focus:ring-crit-solid/30";

export const toolbarButton =
  "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none disabled:opacity-50 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white";

export const primaryButton =
  "inline-flex items-center justify-center gap-1.5 rounded-lg bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-500 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 dark:focus-visible:ring-offset-slate-900";

export const subtleButton =
  "inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none disabled:opacity-50 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white";

export const panelCard =
  "rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900";

/** Tool chips: one hue per kind of reference, the same on the canvas and in the picker. */
export const toolTone: Record<"mcp" | "a2a" | "catalog", string> = {
  mcp: "bg-sky-50 text-sky-800 ring-sky-200 dark:bg-sky-950/60 dark:text-sky-200 dark:ring-sky-900",
  a2a: "bg-violet-50 text-violet-800 ring-violet-200 dark:bg-violet-950/60 dark:text-violet-200 dark:ring-violet-900",
  catalog:
    "bg-slate-100 text-slate-700 ring-slate-200 dark:bg-slate-800 dark:text-slate-200 dark:ring-slate-700",
};

export const runTone: Record<RunStatus, StatusTone> = {
  starting: "running",
  running: "running",
  succeeded: "ok",
  failed: "crit",
  cancelled: "refused",
  budget_exhausted: "warn",
  lost: "neutral",
};

export const runLabel: Record<RunStatus, string> = {
  starting: "starting",
  running: "running",
  succeeded: "succeeded",
  failed: "failed",
  cancelled: "cancelled",
  budget_exhausted: "budget exhausted",
  lost: "outcome unknown",
};

export const nodeTone: Record<NodeRunStatus, StatusTone> = {
  pending: "neutral",
  active: "running",
  ok: "ok",
  failed: "crit",
  cancelled: "refused",
  budget_exhausted: "warn",
  unknown: "neutral",
};

export const nodeLabel: Record<NodeRunStatus, string> = {
  pending: "waiting",
  active: "running",
  ok: "done",
  failed: "failed",
  cancelled: "cancelled",
  budget_exhausted: "budget hit",
  unknown: "unknown",
};

export const budgetLabel: Record<string, string> = {
  max_tokens: "Tokens",
  max_tool_calls: "Tool calls",
  max_delegations: "Delegations",
  max_remote_calls: "Remote calls",
  timeout_s: "Time (s)",
};

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const plain = new Intl.NumberFormat("en");

/** 1204 → "1.2K"; small numbers as they are. */
export function formatCount(n: number): string {
  return n < 10_000 ? plain.format(n) : compact.format(n);
}

export function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatBytes(n: number): string {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
}

/** A digest's first 12 hex characters. */
export function shortDigest(digest: string | null | undefined): string {
  return digest ? digest.slice(0, 12) : "—";
}
