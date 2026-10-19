import { useEffect, useState } from "react";
import type { CatalogResponse, DeclarativeAgent } from "../api/types";
import { guardDot, guardLabel } from "./guards";
import { CloseIcon, SearchIcon } from "./icons";

interface Props {
  /** False while the dashboard is hidden: Escape then belongs to the chat. */
  active: boolean;
  initial: DeclarativeAgent;
  tools: CatalogResponse["tools"];
  onDone: (agent: DeclarativeAgent) => void;
  onCancel: () => void;
}

const input =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 focus:outline-none dark:border-slate-700 dark:bg-slate-950";

/**
 * Compose a declarative agent from existing tools, in a dialog over the
 * dashboard: the form needs more room than a card, and the agent list behind
 * it is what the operator is editing, so it stays in view.
 */
export function AgentForm({ active, initial, tools, onDone, onCancel }: Props) {
  const [draft, setDraft] = useState(initial);
  const [filter, setFilter] = useState("");
  const needle = filter.toLowerCase();
  const shown = tools.filter((t) => t.ref.includes(needle) || t.specialist.includes(needle));
  const set = (patch: Partial<DeclarativeAgent>) => setDraft((d) => ({ ...d, ...patch }));
  const toggle = (ref: string) =>
    set({
      tools: draft.tools.includes(ref)
        ? draft.tools.filter((t) => t !== ref)
        : [...draft.tools, ref],
    });

  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, onCancel]);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-950/40 p-4 backdrop-blur-sm sm:items-center">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-form-title"
        className="w-full max-w-2xl rounded-2xl border border-slate-200 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-900"
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3.5 dark:border-slate-800">
          <h3 id="agent-form-title" className="font-semibold text-slate-900 dark:text-slate-100">
            {initial.name ? `Edit ${initial.name}` : "New composed agent"}
          </h3>
          <button
            type="button"
            aria-label="Close"
            onClick={onCancel}
            className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          >
            <CloseIcon />
          </button>
        </div>
        <form
          aria-label="Agent editor"
          className="flex flex-col gap-4 p-5"
          onSubmit={(e) => {
            e.preventDefault();
            onDone(draft);
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-medium text-slate-600 dark:text-slate-300">
              Name
              <input
                className={`${input} mt-1 font-mono`}
                value={draft.name}
                onChange={(e) => set({ name: e.target.value })}
                placeholder="capacity_planner"
                required
              />
            </label>
            <label className="text-xs font-medium text-slate-600 dark:text-slate-300">
              Description{" "}
              <span className="font-normal text-slate-400">— what the coordinator routes on</span>
              <input
                className={`${input} mt-1`}
                value={draft.description}
                onChange={(e) => set({ description: e.target.value })}
                required
              />
            </label>
          </div>
          <label className="text-xs font-medium text-slate-600 dark:text-slate-300">
            Instruction
            <textarea
              className={`${input} mt-1`}
              rows={3}
              value={draft.instruction}
              onChange={(e) => set({ instruction: e.target.value })}
              required
            />
          </label>
          <fieldset className="text-xs font-medium text-slate-600 dark:text-slate-300">
            <legend>Tools ({draft.tools.length} selected)</legend>
            <div className="relative mt-1">
              <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-slate-400" />
              <input
                className={`${input} pl-8`}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter by tool or specialist…"
                aria-label="Filter tools"
              />
            </div>
            <ul className="orrery-scroll mt-2 max-h-64 overflow-y-auto rounded-lg border border-slate-200 p-1 dark:border-slate-800">
              {shown.map((t) => (
                <li key={t.ref}>
                  <label className="flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 hover:bg-slate-50 dark:hover:bg-slate-800">
                    <input
                      type="checkbox"
                      className="accent-brand-600"
                      checked={draft.tools.includes(t.ref)}
                      onChange={() => toggle(t.ref)}
                    />
                    <span className="font-mono text-[12px] font-normal">{t.ref}</span>
                    <span className="text-[11px] font-normal text-slate-400">{t.specialist}</span>
                    <span
                      className="ml-auto inline-flex items-center gap-1.5 text-[11px] font-normal text-slate-500 dark:text-slate-400"
                      title={t.reason || undefined}
                    >
                      <span className={`h-1.5 w-1.5 rounded-full ${guardDot[t.guard]}`} />
                      {guardLabel[t.guard]}
                    </span>
                  </label>
                </li>
              ))}
              {shown.length === 0 ? (
                <li className="px-2 py-3 text-center font-normal text-slate-400">
                  No tool matches “{filter}”.
                </li>
              ) : null}
            </ul>
          </fieldset>
          <div className="flex justify-end gap-2 border-t border-slate-200 pt-4 dark:border-slate-800">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!draft.tools.length}
              className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-50"
            >
              Keep agent
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
