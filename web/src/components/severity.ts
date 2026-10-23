import type { TriageSeverity } from "../api/types";

/**
 * Status styling, built on the semantic tokens in styles/index.css
 * (ok / warn / crit / guarded / running / refused). Components ask for a
 * *meaning* here rather than picking Tailwind palette colours, so the same
 * state looks the same everywhere and both themes stay in step.
 */
export type StatusTone = "ok" | "warn" | "crit" | "guarded" | "running" | "refused" | "neutral";

/** Soft pill / chip: tinted background, toned text. */
export const tonePill: Record<StatusTone, string> = {
  ok: "bg-ok-bg text-ok-fg",
  warn: "bg-warn-bg text-warn-fg",
  crit: "bg-crit-bg text-crit-fg",
  guarded: "bg-guarded-bg text-guarded-fg",
  running: "bg-running-bg text-running-fg",
  refused: "bg-refused-bg text-refused-fg",
  neutral: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

/** Outlined chip: pill plus a matching border. */
export const toneChip: Record<StatusTone, string> = {
  ok: "border-ok-border bg-ok-bg text-ok-fg",
  warn: "border-warn-border bg-warn-bg text-warn-fg",
  crit: "border-crit-border bg-crit-bg text-crit-fg",
  guarded: "border-guarded-border bg-guarded-bg text-guarded-fg",
  running: "border-running-border bg-running-bg text-running-fg",
  refused: "border-refused-border bg-refused-bg text-refused-fg",
  neutral:
    "border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-800 dark:bg-slate-800/60 dark:text-slate-300",
};

/** Text (and icon) colour alone. */
export const toneText: Record<StatusTone, string> = {
  ok: "text-ok-fg",
  warn: "text-warn-fg",
  crit: "text-crit-fg",
  guarded: "text-guarded-fg",
  running: "text-running-fg",
  refused: "text-refused-fg",
  neutral: "text-slate-600 dark:text-slate-300",
};

/** Solid fill, for dots and accents. */
export const toneSolid: Record<StatusTone, string> = {
  ok: "bg-ok-solid",
  warn: "bg-warn-solid",
  crit: "bg-crit-solid",
  guarded: "bg-guarded-solid",
  running: "bg-running-solid",
  refused: "bg-refused-solid",
  neutral: "bg-slate-500 dark:bg-slate-400",
};

/** Border colour alone (cards that carry a status). */
export const toneBorder: Record<StatusTone, string> = {
  ok: "border-ok-border",
  warn: "border-warn-border",
  crit: "border-crit-border",
  guarded: "border-guarded-border",
  running: "border-running-border",
  refused: "border-refused-border",
  neutral: "border-slate-200 dark:border-slate-800",
};

/** Triage verdict → tone. */
export const severityTone: Record<TriageSeverity, StatusTone> = {
  healthy: "ok",
  degraded: "warn",
  critical: "crit",
};

/** Tailwind classes for a severity badge/pill, per verdict. */
export const severityBadge: Record<TriageSeverity, string> = {
  healthy: tonePill.ok,
  degraded: tonePill.warn,
  critical: tonePill.crit,
};

/** Left-border accent for the triage panel, per verdict. */
export const severityAccent: Record<TriageSeverity, string> = {
  healthy: "border-l-ok-solid",
  degraded: "border-l-warn-solid",
  critical: "border-l-crit-solid",
};

/** Statuses a gate answers with: the tool never ran, by design. */
const REFUSED_STATUSES = new Set([
  "blocked",
  "access_denied",
  "denied",
  "circuit_open",
  "call_budget_exhausted",
  "delegation_budget_exhausted",
  "repeated_failure",
]);

const AWAITING_STATUSES = new Set(["awaiting_confirmation", "confirmation_required"]);

/** Classify a free-text tool-call status into a tone. */
export function statusTone(status: string): StatusTone {
  const s = status.toLowerCase();
  if (s === "success" || s === "ok" || s === "healthy") return "ok";
  if (AWAITING_STATUSES.has(s)) return "warn";
  if (REFUSED_STATUSES.has(s)) return "refused";
  if (s.includes("error") || s.includes("fail") || s === "critical") return "crit";
  return "neutral";
}

/** Classify a free-text tool-call status into a pill style. */
export function statusPill(status: string): string {
  return tonePill[statusTone(status)];
}
