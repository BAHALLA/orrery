import type {
  CatalogResponse,
  CatalogUpdate,
  DeploymentInfo,
  InventoryAgent,
} from "../../api/types";
import { AgentForm } from "../AgentForm";
import { copyName } from "../agentDraft";
import { GUARD_ORDER, guardDot, guardLabel } from "../guards";
import { PlusIcon, SearchIcon } from "../icons";
import { AgentCard } from "./AgentCard";
import { ComposedCard } from "./ComposedCard";
import { DeploymentPanel } from "./DeploymentPanel";
import { panel, sectionTitle } from "./styles";
import { EMPTY_AGENT, useAgentsDraft } from "./useAgentsDraft";

/** The Agents page itself, over a draft seeded from one catalog version. */
export function Dashboard({
  agents,
  deployment,
  catalog,
  active,
  onDirtyChange,
  savedVersion,
  carried,
  saveError,
  onDiscard,
  onSave,
}: {
  agents: InventoryAgent[];
  deployment: DeploymentInfo | null;
  catalog: CatalogResponse | null;
  active: boolean;
  onDirtyChange: (dirty: boolean) => void;
  savedVersion: number | null;
  carried: CatalogUpdate | null;
  saveError: string | null;
  onDiscard: () => void;
  onSave: (update: CatalogUpdate) => Promise<void>;
}) {
  const {
    editable,
    savedAgents,
    composed,
    editing,
    setEditing,
    saving,
    query,
    setQuery,
    show,
    setShow,
    needle,
    dirty,
    changes,
    live,
    counts,
    visible,
    excluded,
    switchable,
    takenNames,
    toggle,
    setAll,
    keep,
    remove,
    save,
    discard,
  } = useAgentsDraft({ agents, catalog, carried, onDirtyChange, onDiscard, onSave });

  return (
    <div className="orrery-scroll relative flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-6 sm:px-6">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
              Agents
            </h2>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              What the coordinator can delegate to, and how each tool is guarded.
            </p>
          </div>
          {editable ? (
            <button
              type="button"
              onClick={() => setEditing({ index: null, agent: EMPTY_AGENT })}
              className="inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-500"
            >
              <PlusIcon /> New agent
            </button>
          ) : null}
        </div>

        {savedVersion !== null && savedVersion === catalog?.catalog.version ? (
          <p
            role="status"
            className="rounded-lg border border-ok-border bg-ok-bg px-3 py-2 text-sm text-ok-fg"
          >
            Saved as version {savedVersion} — new turns use these agents.
          </p>
        ) : null}

        <DeploymentPanel deployment={deployment} catalog={catalog} />

        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Agents served" value={live.length} />
          <Stat label="Tools" value={counts.tools} />
          <Stat label="Need approval" value={counts.confirm} dot={guardDot.confirm} />
          <Stat label="Destructive" value={counts.destructive} dot={guardDot.destructive} />
        </dl>

        {!editable ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Enable or disable agents with <code>ORRERY_SPECIALISTS</code> and add your own in{" "}
            <code>ORRERY_AGENTS_CONFIG</code>.
          </p>
        ) : null}

        <section aria-labelledby="specialists-title" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <h3 id="specialists-title" className={sectionTitle}>
                Specialists
              </h3>
              {editable && switchable.length > 1 ? (
                <div className="flex items-center gap-2 text-xs">
                  <button
                    type="button"
                    onClick={() => setAll(true)}
                    className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                  >
                    All on
                  </button>
                  <span className="text-slate-300 dark:text-slate-700">·</span>
                  <button
                    type="button"
                    onClick={() => setAll(false)}
                    className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                  >
                    All off
                  </button>
                </div>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <ul className="hidden items-center gap-3 text-xs text-slate-500 lg:flex dark:text-slate-400">
                {GUARD_ORDER.filter((g) => g !== "dynamic").map((g) => (
                  <li key={g} className="inline-flex items-center gap-1.5">
                    <span className={`h-1.5 w-1.5 rounded-full ${guardDot[g]}`} />
                    {guardLabel[g]}
                  </li>
                ))}
              </ul>
              {editable ? (
                <div
                  role="group"
                  aria-label="Show agents"
                  className="inline-flex rounded-lg border border-slate-300 p-0.5 text-xs dark:border-slate-700"
                >
                  {(["all", "on", "off"] as const).map((s) => (
                    <button
                      key={s}
                      type="button"
                      aria-pressed={show === s}
                      onClick={() => setShow(s)}
                      className={`rounded-md px-2.5 py-1 font-medium capitalize ${
                        show === s
                          ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                          : "text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
                      }`}
                    >
                      {s}
                    </button>
                  ))}
                </div>
              ) : null}
              <div className="relative">
                <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-slate-500" />
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Find an agent or tool…"
                  aria-label="Find an agent or tool"
                  className="w-56 rounded-lg border border-slate-300 bg-white py-1.5 pr-2.5 pl-8 text-sm focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 focus:outline-none dark:border-slate-700 dark:bg-slate-900"
                />
              </div>
            </div>
          </div>
          <ul className="grid gap-4 lg:grid-cols-2" aria-label="Agents">
            {visible.map((c) => (
              <AgentCard
                key={c.name}
                card={c}
                onToggle={c.switchKey ? () => toggle(c.switchKey as string) : undefined}
                onCompose={
                  editable && c.switchKey
                    ? () =>
                        setEditing({
                          index: null,
                          agent: EMPTY_AGENT,
                          specialist: c.switchKey,
                        })
                    : undefined
                }
              />
            ))}
          </ul>
          {visible.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">
              {needle ? `No agent or tool matches “${query}”.` : `No agent is ${show}.`}
            </p>
          ) : null}
          {excluded.length ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">
              Not in this deployment:{" "}
              {excluded.map((s, i) => (
                <span key={s.key}>
                  {i ? ", " : ""}
                  <span className="font-mono">{s.key}</span>
                </span>
              ))}{" "}
              — add them to <code>ORRERY_SPECIALISTS</code> to make them switchable here.
            </p>
          ) : null}
        </section>

        {editable && catalog ? (
          <section aria-labelledby="composed-title" className="flex flex-col gap-3">
            <div>
              <h3 id="composed-title" className={sectionTitle}>
                Composed agents
              </h3>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                A brief over existing tools — no new code, the same guards.
              </p>
            </div>
            <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {catalog.file_agents.map((name) => (
                <li
                  key={name}
                  className="rounded-xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-900/50"
                >
                  <p className="font-mono text-sm font-medium text-slate-700 dark:text-slate-200">
                    {name}
                  </p>
                  <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                    From the deployment's config file — fixed here.
                  </p>
                </li>
              ))}
              {composed.map((a, i) => (
                <ComposedCard
                  key={a.name || i}
                  agent={a}
                  saved={savedAgents.find((s) => s.name === a.name)}
                  tools={catalog.tools}
                  onEdit={() => setEditing({ index: i, agent: a })}
                  onDuplicate={() =>
                    setEditing({
                      index: null,
                      agent: { ...a, name: copyName(a.name, takenNames(null)) },
                    })
                  }
                  onRemove={() => remove(i)}
                />
              ))}
              <li>
                <button
                  type="button"
                  onClick={() => setEditing({ index: null, agent: EMPTY_AGENT })}
                  className="flex h-full min-h-28 w-full flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed border-slate-300 px-4 py-5 text-sm text-slate-500 hover:border-brand-400 hover:text-brand-700 dark:border-slate-700 dark:hover:text-brand-300"
                >
                  <PlusIcon />
                  {composed.length ? "Compose another" : "Compose your first agent"}
                  <span className="text-xs">
                    A focused brief over tools the coordinator already has.
                  </span>
                </button>
              </li>
            </ul>
          </section>
        ) : null}
      </div>

      {editable && (dirty || saveError) ? (
        <div className="sticky bottom-0 mt-auto border-t border-slate-200 bg-white/90 backdrop-blur dark:border-slate-800 dark:bg-slate-900/90">
          <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
            {saveError ? (
              <p role="alert" className="min-w-0 flex-1 text-sm text-crit-fg">
                {saveError}
              </p>
            ) : (
              <p className="min-w-0 flex-1 text-sm text-slate-600 dark:text-slate-300">
                <span className="font-medium">Unsaved:</span> {changes ?? "reordered"} — nothing is
                served until you save.
              </p>
            )}
            <button
              type="button"
              onClick={discard}
              className="rounded-lg px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              Discard
            </button>
            <button
              type="button"
              disabled={!dirty || saving}
              onClick={() => void save()}
              className="rounded-lg bg-brand-600 px-4 py-1.5 text-sm font-medium whitespace-nowrap text-white hover:bg-brand-500 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </div>
      ) : null}

      {editing && catalog ? (
        <AgentForm
          active={active}
          initial={editing.agent}
          tools={catalog.tools}
          specialist={editing.specialist}
          taken={takenNames(
            editing.index === null ? null : (composed[editing.index]?.name ?? null),
          )}
          onCancel={() => setEditing(null)}
          onDone={keep}
        />
      ) : null}
    </div>
  );
}

function Stat({ label, value, dot }: { label: string; value: number; dot?: string }) {
  return (
    <div className={panel}>
      <dt className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
        {dot ? <span className={`h-1.5 w-1.5 rounded-full ${dot}`} /> : null}
        {label}
      </dt>
      <dd className="mt-1 text-2xl font-semibold text-slate-900 tabular-nums dark:text-slate-50">
        {value}
      </dd>
    </div>
  );
}
