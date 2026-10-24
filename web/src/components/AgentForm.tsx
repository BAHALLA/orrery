import { useEffect, useMemo, useRef, useState } from "react";
import type { CatalogResponse, DeclarativeAgent } from "../api/types";
import { guardCounts, INSTRUCTION_TEMPLATE, nameProblem, toolsBySpecialist } from "./agentDraft";
import { guardDot, guardLabel } from "./guards";
import { CloseIcon, SearchIcon } from "./icons";

interface Props {
  /** False while the dashboard is hidden: Escape then belongs to the chat. */
  active: boolean;
  initial: DeclarativeAgent;
  tools: CatalogResponse["tools"];
  /** Every other agent's name: a new name must not be one of them. */
  taken: ReadonlySet<string>;
  /** Open with the tool list narrowed to one specialist ("compose from…"). */
  specialist?: string | undefined;
  onDone: (agent: DeclarativeAgent) => void;
  onCancel: () => void;
}

type Tool = CatalogResponse["tools"][number];

const input =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 focus:outline-none dark:border-slate-700 dark:bg-slate-950";

const MAX_DESCRIPTION = 200;
/** The server's floor for a description and an instruction (`DeclarativeAgentConfig`). */
const MIN_TEXT = 10;

const tooShort = (text: string, what: string) =>
  text.trim().length < MIN_TEXT ? `${what} needs at least ${MIN_TEXT} characters.` : null;

/**
 * Compose a declarative agent from existing tools, in a dialog over the
 * dashboard: the form needs more room than a card, and the agent list behind
 * it is what the operator is editing, so it stays in view.
 *
 * It checks what the server would refuse (the name rule, a name in use or
 * reserved, a description or instruction under 10 characters, no tools) as
 * the admin types, so "Keep agent" never queues a draft that the
 * save then rejects with a 422.
 */
export function AgentForm({ active, initial, tools, taken, specialist, onDone, onCancel }: Props) {
  const [draft, setDraft] = useState(initial);
  const [filter, setFilter] = useState(specialist ?? "");
  const [touched, setTouched] = useState(false);
  const set = (patch: Partial<DeclarativeAgent>) => setDraft((d) => ({ ...d, ...patch }));
  const selected = new Set(draft.tools);
  const toggle = (refs: string[], on: boolean) =>
    set({
      tools: on
        ? [...draft.tools, ...refs.filter((r) => !selected.has(r))]
        : draft.tools.filter((t) => !refs.includes(t)),
    });

  const needle = filter.trim().toLowerCase();
  const groups = useMemo(
    () =>
      toolsBySpecialist(tools)
        .map(([key, ts]) => {
          const shown = key.includes(needle) ? ts : ts.filter((t) => t.ref.includes(needle));
          return [key, shown] as const;
        })
        .filter(([, ts]) => ts.length > 0),
    [tools, needle],
  );

  const problem = nameProblem(draft.name, taken);
  const counts = guardCounts(draft.tools, tools);
  const descriptionProblem = tooShort(draft.description, "The description");
  const instructionProblem = tooShort(draft.instruction, "The instruction");
  const ready = !problem && !descriptionProblem && !instructionProblem && selected.size > 0;

  const submit = () => {
    setTouched(true);
    if (ready) onDone({ ...draft, description: draft.description.trim() });
  };

  // A modal keeps focus: Tab cycles inside it, and closing returns focus to
  // the button that opened it.
  const dialog = useRef<HTMLDivElement>(null);
  // Read during the first render: by the time an effect runs, autoFocus has
  // already moved focus to the name field.
  const [opener] = useState(() => document.activeElement as HTMLElement | null);
  useEffect(() => () => opener?.focus?.(), [opener]);
  const trapTab = (e: React.KeyboardEvent) => {
    if (e.key !== "Tab" || !dialog.current) return;
    const focusable = dialog.current.querySelectorAll<HTMLElement>(
      "button:not([disabled]), input:not([disabled]), textarea, [tabindex]:not([tabindex='-1'])",
    );
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  };

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
        ref={dialog}
        onKeyDown={trapTab}
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-form-title"
        className="w-full max-w-3xl rounded-2xl border border-slate-200 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-900"
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3.5 dark:border-slate-800">
          <div>
            <h3 id="agent-form-title" className="font-semibold text-slate-900 dark:text-slate-100">
              {initial.name ? `Edit ${initial.name}` : "New composed agent"}
            </h3>
            <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
              A brief over tools the coordinator already has. Their guards apply unchanged.
            </p>
          </div>
          <button
            type="button"
            aria-label="Close"
            onClick={onCancel}
            className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          >
            <CloseIcon />
          </button>
        </div>
        <form
          aria-label="Agent editor"
          className="flex flex-col gap-4 p-5"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              submit();
            }
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="text-xs font-medium text-slate-600 dark:text-slate-300">
              <label htmlFor="agent-name">Name</label>
              <input
                id="agent-name"
                className={`${input} mt-1 font-mono`}
                value={draft.name}
                onChange={(e) =>
                  set({ name: e.target.value.toLowerCase().replace(/[\s-]+/g, "_") })
                }
                onBlur={() => setTouched(true)}
                placeholder="capacity_planner"
                aria-invalid={touched && problem !== null}
                aria-describedby="agent-name-help"
                autoFocus
                required
              />
              <p
                id="agent-name-help"
                className={`mt-1 font-normal ${touched && problem ? "text-crit-fg" : "text-slate-500"}`}
              >
                {touched && problem
                  ? problem
                  : "How the coordinator calls it; lowercase_with_underscores."}
              </p>
            </div>
            <div className="text-xs font-medium text-slate-600 dark:text-slate-300">
              <label htmlFor="agent-description">
                Description{" "}
                <span className="font-normal text-slate-500">— what the coordinator routes on</span>
              </label>
              <input
                id="agent-description"
                className={`${input} mt-1`}
                value={draft.description}
                maxLength={MAX_DESCRIPTION}
                onChange={(e) => set({ description: e.target.value })}
                placeholder="Forecasts disk and CPU headroom for a namespace"
                aria-invalid={touched && descriptionProblem !== null}
                aria-describedby="agent-description-help"
                required
              />
              <p
                id="agent-description-help"
                className={`mt-1 flex justify-between gap-2 font-normal ${
                  touched && descriptionProblem ? "text-crit-fg" : "text-slate-500"
                }`}
              >
                <span>{touched && descriptionProblem ? descriptionProblem : ""}</span>
                <span className="tabular-nums">
                  {draft.description.length}/{MAX_DESCRIPTION}
                </span>
              </p>
            </div>
          </div>

          <div className="text-xs font-medium text-slate-600 dark:text-slate-300">
            <div className="flex items-center justify-between">
              <label htmlFor="agent-instruction">Instruction</label>
              {!draft.instruction ? (
                <button
                  type="button"
                  onClick={() => set({ instruction: INSTRUCTION_TEMPLATE })}
                  className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                >
                  Start from a template
                </button>
              ) : null}
            </div>
            <textarea
              id="agent-instruction"
              className={`${input} mt-1 font-mono text-[13px] leading-relaxed`}
              rows={7}
              value={draft.instruction}
              onChange={(e) => set({ instruction: e.target.value })}
              placeholder="What this agent is for, and how it should work."
              aria-invalid={touched && instructionProblem !== null}
              aria-describedby={
                touched && instructionProblem ? "agent-instruction-help" : undefined
              }
              required
            />
            {touched && instructionProblem ? (
              <p id="agent-instruction-help" className="mt-1 font-normal text-crit-fg">
                {instructionProblem}
              </p>
            ) : null}
          </div>

          <fieldset className="text-xs font-medium text-slate-600 dark:text-slate-300">
            <legend>Tools ({selected.size} selected)</legend>
            {selected.size ? (
              <ul className="mt-1.5 flex flex-wrap gap-1" aria-label="Selected tools">
                {draft.tools.map((ref) => (
                  <li key={ref}>
                    <button
                      type="button"
                      aria-label={`Remove ${ref}`}
                      onClick={() => toggle([ref], false)}
                      className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-slate-50 py-0.5 pr-1 pl-1.5 font-mono text-[11px] font-normal hover:border-crit-border hover:text-crit-fg dark:border-slate-700 dark:bg-slate-800"
                    >
                      {ref}
                      <CloseIcon size={12} />
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
            {counts.confirm || counts.destructive ? (
              <p className="mt-2 rounded-lg bg-guarded-bg px-2.5 py-1.5 font-normal text-guarded-fg">
                {[
                  counts.confirm ? `${counts.confirm} need approval` : "",
                  counts.destructive ? `${counts.destructive} destructive` : "",
                ]
                  .filter(Boolean)
                  .join(" · ")}{" "}
                — the coordinator still asks before running them, and RBAC still applies.
              </p>
            ) : null}
            <div className="relative mt-2">
              <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-slate-500" />
              <input
                className={`${input} pl-8`}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                // Enter here filters; it must not keep the agent. Ctrl/⌘+Enter
                // still reaches the form's own handler.
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.metaKey && !e.ctrlKey) e.preventDefault();
                }}
                placeholder="Filter by tool or specialist…"
                aria-label="Filter tools"
              />
            </div>
            <div className="orrery-scroll mt-2 max-h-72 overflow-y-auto rounded-lg border border-slate-200 dark:border-slate-800">
              {groups.map(([key, ts]) => (
                <ToolGroup
                  key={key}
                  specialist={key}
                  tools={ts}
                  selected={selected}
                  onToggle={toggle}
                />
              ))}
              {groups.length === 0 ? (
                <p className="px-2 py-3 text-center font-normal text-slate-500">
                  No tool matches “{filter}”.
                </p>
              ) : null}
            </div>
          </fieldset>

          <div className="flex items-center justify-end gap-2 border-t border-slate-200 pt-4 dark:border-slate-800">
            <p className="mr-auto hidden text-[11px] text-slate-500 sm:block">
              <kbd className="rounded border border-slate-300 px-1 dark:border-slate-700">Ctrl</kbd>{" "}
              +{" "}
              <kbd className="rounded border border-slate-300 px-1 dark:border-slate-700">
                Enter
              </kbd>{" "}
              to keep · nothing is served until you save the page
            </p>
            <button
              type="button"
              onClick={onCancel}
              className="rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!ready}
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

function ToolGroup({
  specialist,
  tools,
  selected,
  onToggle,
}: {
  specialist: string;
  tools: readonly Tool[];
  selected: ReadonlySet<string>;
  onToggle: (refs: string[], on: boolean) => void;
}) {
  const refs = tools.map((t) => t.ref);
  const chosen = refs.filter((r) => selected.has(r)).length;
  const all = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (all.current) all.current.indeterminate = chosen > 0 && chosen < refs.length;
  }, [chosen, refs.length]);

  return (
    <div className="border-b border-slate-100 last:border-b-0 dark:border-slate-800">
      <label className="sticky top-0 flex cursor-pointer items-center gap-2.5 bg-slate-50 px-3 py-1.5 dark:bg-slate-800/60">
        <input
          ref={all}
          type="checkbox"
          className="accent-brand-600"
          checked={chosen === refs.length}
          onChange={(e) => onToggle(refs, e.target.checked)}
          aria-label={`All ${specialist} tools`}
        />
        <span className="font-mono text-[12px] font-semibold">{specialist}</span>
        <span className="font-normal text-slate-500 tabular-nums">
          {chosen}/{refs.length}
        </span>
      </label>
      <ul className="p-1">
        {tools.map((t) => (
          <li key={t.ref}>
            <label className="flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 hover:bg-slate-50 dark:hover:bg-slate-800">
              <input
                type="checkbox"
                className="accent-brand-600"
                checked={selected.has(t.ref)}
                onChange={() => onToggle([t.ref], !selected.has(t.ref))}
              />
              <span className="font-mono text-[12px] font-normal">{t.ref}</span>
              <span className="text-[11px] font-normal text-slate-500">{t.specialist}</span>
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
      </ul>
    </div>
  );
}
