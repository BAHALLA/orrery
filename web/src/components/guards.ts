import type { ToolGuard } from "../api/types";

/** Display order: the tools that need the most care first. */
export const GUARD_ORDER: readonly ToolGuard[] = ["destructive", "confirm", "dynamic", "read"];

export const guardLabel: Record<ToolGuard, string> = {
  read: "Read-only",
  confirm: "Needs approval",
  destructive: "Destructive",
  dynamic: "MCP, per tool",
};

export const guardTitle: Record<ToolGuard, string> = {
  read: "Read-only — any role",
  confirm: "Changes state — operator, after approval",
  destructive: "Destructive — admin, after approval",
  dynamic: "MCP toolset — each tool classified when discovered",
};

/** One small coloured dot per guard: enough to scan by, quiet enough to read past. */
export const guardDot: Record<ToolGuard, string> = {
  read: "bg-slate-400 dark:bg-slate-500",
  confirm: "bg-amber-500",
  destructive: "bg-red-500",
  dynamic: "bg-brand-500",
};

export const guardChip: Record<ToolGuard, string> = {
  read: "border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-800 dark:bg-slate-800/60 dark:text-slate-300",
  confirm:
    "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-200",
  destructive:
    "border-red-200 bg-red-50 text-red-800 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-200",
  dynamic:
    "border-brand-200 bg-brand-50 text-brand-800 dark:border-brand-900/50 dark:bg-brand-950/30 dark:text-brand-200",
};
