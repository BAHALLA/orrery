/** Class strings and labels shared by the Agents page's pieces. */

export const sectionTitle =
  "text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400";

export const pill =
  "rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300";

/** The outline of a card whose change waits for "Save changes". */
export const pendingBorder = "border-brand-400 ring-1 ring-brand-400/40";

/** The badge naming that pending change. */
export const pendingBadge =
  "rounded-full bg-brand-50 px-2 py-0.5 text-[11px] font-medium text-brand-700 dark:bg-brand-900/40 dark:text-brand-300";

/** A plain white panel: the deployment strip and each stat. */
export const panel =
  "rounded-xl border border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900";

export const sourceLabel: Record<string, string> = {
  builtin: "built-in",
  plugin: "plugin",
  declarative: "declarative",
  mcp: "MCP",
};
