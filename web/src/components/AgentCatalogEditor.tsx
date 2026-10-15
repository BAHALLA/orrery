import { useMemo, useState } from "react";
import type { CatalogResponse, CatalogUpdate, DeclarativeAgent } from "../api/types";

interface Props {
  catalog: CatalogResponse;
  /** Resolves to an error message, or null when saved. The parent keys this
   * editor on the catalog version, so a successful save re-seeds the draft. */
  onSave: (update: CatalogUpdate) => Promise<string | null>;
}

const guardChip: Record<string, string> = {
  read: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  confirm: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  destructive: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
};

const input =
  "w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 focus:outline-none dark:border-slate-700 dark:bg-slate-900";

const EMPTY: DeclarativeAgent = { name: "", description: "", instruction: "", tools: [] };

function AgentForm({
  initial,
  tools,
  onDone,
  onCancel,
}: {
  initial: DeclarativeAgent;
  tools: CatalogResponse["tools"];
  onDone: (agent: DeclarativeAgent) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const [filter, setFilter] = useState("");
  const shown = tools.filter(
    (t) => t.ref.includes(filter.toLowerCase()) || t.specialist.includes(filter.toLowerCase()),
  );
  const set = (patch: Partial<DeclarativeAgent>) => setDraft((d) => ({ ...d, ...patch }));
  const toggle = (ref: string) =>
    set({
      tools: draft.tools.includes(ref)
        ? draft.tools.filter((t) => t !== ref)
        : [...draft.tools, ref],
    });

  return (
    <form
      aria-label="Agent editor"
      className="flex flex-col gap-2 rounded-xl border border-brand-200 bg-brand-50/40 p-3 dark:border-brand-900 dark:bg-brand-950/20"
      onSubmit={(e) => {
        e.preventDefault();
        onDone(draft);
      }}
    >
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
        <input
          className={`${input} mt-1`}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter tools…"
          aria-label="Filter tools"
        />
        <ul className="orrery-scroll mt-1 max-h-48 overflow-y-auto rounded-lg border border-slate-200 bg-white p-1 dark:border-slate-800 dark:bg-slate-900">
          {shown.map((t) => (
            <li key={t.ref}>
              <label className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 hover:bg-slate-50 dark:hover:bg-slate-800">
                <input
                  type="checkbox"
                  checked={draft.tools.includes(t.ref)}
                  onChange={() => toggle(t.ref)}
                />
                <span className="font-mono text-[12px] font-normal">{t.ref}</span>
                <span className="text-[11px] font-normal text-slate-400">{t.specialist}</span>
                <span
                  className={`ml-auto rounded px-1 text-[10px] font-semibold uppercase ${guardChip[t.guard]}`}
                  title={t.reason || undefined}
                >
                  {t.guard}
                </span>
              </label>
            </li>
          ))}
        </ul>
      </fieldset>
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-lg px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={!draft.tools.length}
          className="rounded-lg bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-50"
        >
          Keep agent
        </button>
      </div>
    </form>
  );
}

/**
 * Edit the agent catalog: switch specialists off and compose declarative
 * agents from existing tools. Edits stay local until "Save changes", which
 * sends the whole catalog with the version it was loaded at — the server
 * builds it first (refusing anything that won't build) and rejects the save if
 * someone else changed the catalog meanwhile.
 */
export function AgentCatalogEditor({ catalog, onSave }: Props) {
  const [disabled, setDisabled] = useState<string[]>(catalog.catalog.disabled_specialists);
  const [agents, setAgents] = useState<DeclarativeAgent[]>(catalog.catalog.agents);
  const [editing, setEditing] = useState<{ index: number | null; agent: DeclarativeAgent } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const dirty = useMemo(
    () =>
      JSON.stringify([disabled, agents]) !==
      JSON.stringify([catalog.catalog.disabled_specialists, catalog.catalog.agents]),
    [disabled, agents, catalog],
  );

  const save = async () => {
    setSaving(true);
    setError(null);
    const failure = await onSave({
      version: catalog.catalog.version,
      disabled_specialists: disabled,
      agents,
    });
    // On success this component is remounted with the new version; only a
    // failure needs reporting from here.
    if (failure) {
      setSaving(false);
      setError(failure);
    }
  };

  return (
    <section aria-label="Edit agents" className="flex flex-col gap-3">
      <div>
        <h3 className="mb-1 text-xs font-semibold tracking-wide text-slate-500 uppercase dark:text-slate-400">
          Specialists
        </h3>
        <ul className="flex flex-col gap-1">
          {catalog.specialists.map((s) => {
            const on = s.available && !disabled.includes(s.key);
            return (
              <li key={s.key} className="flex items-center gap-2 text-sm">
                <button
                  type="button"
                  role="switch"
                  aria-checked={on}
                  aria-label={`Enable ${s.key}`}
                  disabled={!s.available}
                  onClick={() =>
                    setDisabled((d) =>
                      d.includes(s.key) ? d.filter((k) => k !== s.key) : [...d, s.key],
                    )
                  }
                  className={`relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-40 ${on ? "bg-brand-600" : "bg-slate-300 dark:bg-slate-700"}`}
                >
                  <span
                    className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${on ? "left-[18px]" : "left-0.5"}`}
                  />
                </button>
                <span className="font-mono text-[13px]">{s.key}</span>
                {!s.available ? (
                  <span className="text-[11px] text-slate-400">excluded by the deployment</span>
                ) : null}
              </li>
            );
          })}
        </ul>
      </div>

      <div>
        <h3 className="mb-1 text-xs font-semibold tracking-wide text-slate-500 uppercase dark:text-slate-400">
          Composed agents
        </h3>
        <ul className="flex flex-col gap-1">
          {catalog.file_agents.map((name) => (
            <li key={name} className="text-sm text-slate-500 dark:text-slate-400">
              <span className="font-mono">{name}</span> — from the deployment's config file
            </li>
          ))}
          {agents.map((a, i) => (
            <li key={a.name || i} className="flex items-center gap-2 text-sm">
              <span className="font-mono">{a.name}</span>
              <span className="text-[11px] text-slate-400">{a.tools.length} tools</span>
              <button
                type="button"
                className="ml-auto text-xs text-brand-600 hover:underline"
                onClick={() => setEditing({ index: i, agent: a })}
              >
                Edit
              </button>
              <button
                type="button"
                aria-label={`Remove agent ${a.name}`}
                className="text-xs text-red-600 hover:underline"
                onClick={() => setAgents((list) => list.filter((_, j) => j !== i))}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
        {editing ? (
          <div className="mt-2">
            <AgentForm
              initial={editing.agent}
              tools={catalog.tools}
              onCancel={() => setEditing(null)}
              onDone={(agent) => {
                setAgents((list) =>
                  editing.index === null
                    ? [...list, agent]
                    : list.map((a, j) => (j === editing.index ? agent : a)),
                );
                setEditing(null);
              }}
            />
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setEditing({ index: null, agent: EMPTY })}
            className="mt-2 rounded-lg border border-dashed border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:border-brand-400 dark:border-slate-700 dark:text-slate-300"
          >
            New agent
          </button>
        )}
      </div>

      <div className="flex items-center gap-3 border-t border-slate-200 pt-3 dark:border-slate-800">
        <button
          type="button"
          disabled={!dirty || saving}
          onClick={() => void save()}
          className="rounded-lg bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-500 disabled:opacity-50 shrink-0 whitespace-nowrap"
        >
          {saving ? "Saving…" : "Save changes"}
        </button>
        {error ? (
          <p role="alert" className="text-xs text-red-600 dark:text-red-400">
            {error}
          </p>
        ) : (
          <p className="text-xs text-slate-400">
            Version {catalog.catalog.version}
            {catalog.catalog.updated_by ? ` · last changed by ${catalog.catalog.updated_by}` : ""}
          </p>
        )}
      </div>
    </section>
  );
}
