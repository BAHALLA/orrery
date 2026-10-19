import { useEffect, useMemo, useState } from "react";
import type {
  CatalogResponse,
  CatalogUpdate,
  DeclarativeAgent,
  InventoryAgent,
  ToolGuard,
} from "../api/types";
import type { SystemController } from "../system/useSystem";
import { AgentForm } from "./AgentForm";
import { GUARD_ORDER, guardChip, guardDot, guardLabel, guardTitle } from "./guards";
import { PlusIcon, SearchIcon } from "./icons";

interface Props {
  system: SystemController;
  /** False while the console shows the chat: the page stays mounted, hidden,
   * so a draft survives navigating away (or a triage verdict pulling the
   * console back to the chat). */
  active: boolean;
  /** Told whenever the draft gains or loses unsaved changes. */
  onDirtyChange: (dirty: boolean) => void;
}

type Tool = InventoryAgent["tools"][number];

const EMPTY_AGENT: DeclarativeAgent = { name: "", description: "", instruction: "", tools: [] };

const sourceLabel: Record<string, string> = {
  builtin: "built-in",
  plugin: "plugin",
  declarative: "declarative",
  mcp: "MCP",
};

/** Order-insensitive: switching a specialist off and on again reorders the list. */
const sameDraft = (a: string[], b: DeclarativeAgent[], c: string[], d: DeclarativeAgent[]) =>
  JSON.stringify([[...a].sort(), b]) === JSON.stringify([[...c].sort(), d]);

const sectionTitle =
  "text-xs font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400";

/**
 * What the coordinator can reach, for admins, as a full page: each agent with
 * its tools grouped by guard level, and — when the deployment allows editing —
 * a switch per specialist and the composed agents, saved together.
 *
 * Served agents come from the running tree (`/admin/agents`), so a disabled
 * specialist is absent there because it is absent from what runs. The catalog
 * adds what is *not* served: switched-off specialists (tools taken from the
 * catalog's tool list) and the ones the deployment's env excludes.
 */
export function AgentsDashboard({ system, active, onDirtyChange }: Props) {
  const { agents, agentsError, loadAgents, loadCatalog, saveCatalog } = system;
  // Only a well-formed catalog counts: an older server answers with something else.
  const catalog = system.catalog?.catalog ? system.catalog : null;
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (agents === null) void loadAgents();
  }, [agents, loadAgents]);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  if (agentsError) {
    return (
      <div className="p-6">
        <p
          role="alert"
          className="mx-auto max-w-6xl rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/50 dark:text-red-300"
        >
          {agentsError}
        </p>
      </div>
    );
  }
  if (agents === null) {
    return <p className="p-6 text-sm text-slate-500 dark:text-slate-400">Loading agents…</p>;
  }

  return (
    <Dashboard
      // A successful save re-seeds the draft from the new version.
      key={catalog?.catalog.version ?? "read-only"}
      agents={agents}
      catalog={catalog}
      active={active}
      onDirtyChange={onDirtyChange}
      savedVersion={savedVersion}
      saveError={saveError}
      onDiscard={() => setSaveError(null)}
      onSave={async (update) => {
        const error = await saveCatalog(update);
        setSaveError(error);
        if (!error) setSavedVersion(update.version + 1);
      }}
    />
  );
}

interface Card {
  name: string;
  description: string;
  source: string;
  tools: Tool[];
  /** The catalog key, for a specialist the admin can switch. */
  switchKey?: string | undefined;
  on: boolean;
}

function Dashboard({
  agents,
  catalog,
  active,
  onDirtyChange,
  savedVersion,
  saveError,
  onDiscard,
  onSave,
}: {
  agents: InventoryAgent[];
  catalog: CatalogResponse | null;
  active: boolean;
  onDirtyChange: (dirty: boolean) => void;
  savedVersion: number | null;
  saveError: string | null;
  onDiscard: () => void;
  onSave: (update: CatalogUpdate) => Promise<void>;
}) {
  const editable = catalog?.editing_enabled === true;
  const [disabled, setDisabled] = useState<string[]>(catalog?.catalog.disabled_specialists ?? []);
  const [composed, setComposed] = useState<DeclarativeAgent[]>(catalog?.catalog.agents ?? []);
  const [editing, setEditing] = useState<{ index: number | null; agent: DeclarativeAgent } | null>(
    null,
  );
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState("");

  const dirty =
    editable &&
    !sameDraft(
      disabled,
      composed,
      catalog?.catalog.disabled_specialists ?? [],
      catalog?.catalog.agents ?? [],
    );

  // An open agent dialog counts too: its text isn't in the draft until "Keep agent".
  const unsaved = dirty || editing !== null;
  useEffect(() => {
    onDirtyChange(unsaved);
  }, [unsaved, onDirtyChange]);

  const cards = useMemo<Card[]>(() => {
    const served = new Map(agents.map((a) => [a.name, a]));
    const specialists = catalog?.specialists.filter((s) => s.available) ?? [];
    const claimed = new Set(specialists.map((s) => s.agent_name));
    const fromCatalog = specialists.map<Card>((s) => {
      const live = served.get(s.agent_name);
      return {
        name: s.agent_name,
        description: live?.description ?? s.description,
        source: live?.source ?? s.source,
        tools:
          live?.tools ??
          (catalog?.tools ?? [])
            .filter((t) => t.specialist === s.key)
            .map((t) => ({ name: t.ref, guard: t.guard, reason: t.reason })),
        switchKey: editable ? s.key : undefined,
        on: !disabled.includes(s.key),
      };
    });
    // Composed agents get their own section while editing; elsewhere they are
    // just more agents being served.
    const rest = agents
      .filter((a) => !claimed.has(a.name) && !(editable && a.source === "declarative"))
      .map<Card>((a) => ({ ...a, on: true }));
    return [...fromCatalog, ...rest];
  }, [agents, catalog, disabled, editable]);

  const live = cards.filter((c) => c.on);
  const allTools = live.flatMap((c) => c.tools);
  const count = (g: ToolGuard) => allTools.filter((t) => t.guard === g).length;

  const needle = query.trim().toLowerCase();
  const visible = needle
    ? cards.flatMap((c) => {
        if (`${c.name} ${c.description}`.toLowerCase().includes(needle)) return [c];
        const tools = c.tools.filter((t) => t.name.toLowerCase().includes(needle));
        return tools.length ? [{ ...c, tools }] : [];
      })
    : cards;

  const excluded = catalog?.specialists.filter((s) => !s.available) ?? [];

  const save = async () => {
    if (!catalog) return;
    setSaving(true);
    await onSave({
      version: catalog.catalog.version,
      disabled_specialists: disabled,
      agents: composed,
    });
    // On success this component is remounted with the new version.
    setSaving(false);
  };

  const discard = () => {
    setDisabled(catalog?.catalog.disabled_specialists ?? []);
    setComposed(catalog?.catalog.agents ?? []);
    onDiscard();
  };

  return (
    <div className="orrery-scroll flex min-h-0 flex-1 flex-col overflow-y-auto">
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
          {catalog ? (
            <p className="text-xs text-slate-400 dark:text-slate-500">
              Catalog version {catalog.catalog.version}
              {catalog.catalog.updated_by ? ` · last changed by ${catalog.catalog.updated_by}` : ""}
            </p>
          ) : null}
        </div>

        {savedVersion !== null && savedVersion === catalog?.catalog.version ? (
          <p
            role="status"
            className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:border-emerald-900/60 dark:bg-emerald-950/40 dark:text-emerald-300"
          >
            Saved as version {savedVersion} — new turns use these agents.
          </p>
        ) : null}

        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Agents served" value={live.length} />
          <Stat label="Tools" value={allTools.length} />
          <Stat label="Need approval" value={count("confirm")} dot={guardDot.confirm} />
          <Stat label="Destructive" value={count("destructive")} dot={guardDot.destructive} />
        </dl>

        {!editable ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Enable or disable agents with <code>ORRERY_SPECIALISTS</code> and add your own in{" "}
            <code>ORRERY_AGENTS_CONFIG</code>
            {catalog ? (
              <>
                , or set <code>ORRERY_ADMIN_EDITING=true</code> to edit them here
              </>
            ) : null}
            .
          </p>
        ) : null}

        <section aria-labelledby="specialists-title" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 id="specialists-title" className={sectionTitle}>
              Specialists
            </h3>
            <div className="flex flex-wrap items-center gap-4">
              <ul className="hidden items-center gap-3 text-xs text-slate-500 md:flex dark:text-slate-400">
                {GUARD_ORDER.filter((g) => g !== "dynamic").map((g) => (
                  <li key={g} className="inline-flex items-center gap-1.5">
                    <span className={`h-1.5 w-1.5 rounded-full ${guardDot[g]}`} />
                    {guardLabel[g]}
                  </li>
                ))}
              </ul>
              <div className="relative">
                <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-slate-400" />
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
                onToggle={
                  c.switchKey
                    ? () => {
                        const key = c.switchKey as string;
                        setDisabled((d) =>
                          d.includes(key) ? d.filter((k) => k !== key) : [...d, key],
                        );
                      }
                    : undefined
                }
              />
            ))}
          </ul>
          {visible.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-400">
              No agent or tool matches “{query}”.
            </p>
          ) : null}
          {excluded.length ? (
            <p className="text-xs text-slate-400 dark:text-slate-500">
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
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 id="composed-title" className={sectionTitle}>
                  Composed agents
                </h3>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  A brief over existing tools — no new code, the same guards.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setEditing({ index: null, agent: EMPTY_AGENT })}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:border-brand-400 hover:text-brand-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:text-brand-300"
              >
                <PlusIcon /> New agent
              </button>
            </div>
            {composed.length || catalog.file_agents.length ? (
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
                  <li
                    key={a.name || i}
                    className="flex flex-col rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
                  >
                    <p className="font-mono text-sm font-medium text-slate-900 dark:text-slate-100">
                      {a.name}
                    </p>
                    <p
                      className="mt-1 line-clamp-2 text-xs text-slate-500 dark:text-slate-400"
                      title={a.description}
                    >
                      {a.description}
                    </p>
                    <div className="mt-3 flex items-center gap-3 text-xs">
                      <span className="text-slate-400">
                        {a.tools.length} tool{a.tools.length === 1 ? "" : "s"}
                      </span>
                      <button
                        type="button"
                        className="ml-auto font-medium text-brand-600 hover:underline dark:text-brand-400"
                        onClick={() => setEditing({ index: i, agent: a })}
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        aria-label={`Remove agent ${a.name}`}
                        className="font-medium text-red-600 hover:underline dark:text-red-400"
                        onClick={() => setComposed((list) => list.filter((_, j) => j !== i))}
                      >
                        Remove
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="rounded-xl border border-dashed border-slate-300 px-4 py-6 text-center text-sm text-slate-400 dark:border-slate-700">
                None yet. Compose one to give the coordinator a focused brief over tools it already
                has.
              </p>
            )}
          </section>
        ) : null}
      </div>

      {editable && (dirty || saveError) ? (
        <div className="sticky bottom-0 mt-auto border-t border-slate-200 bg-white/90 backdrop-blur dark:border-slate-800 dark:bg-slate-900/90">
          <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
            {saveError ? (
              <p role="alert" className="min-w-0 flex-1 text-sm text-red-600 dark:text-red-400">
                {saveError}
              </p>
            ) : (
              <p className="min-w-0 flex-1 text-sm text-slate-600 dark:text-slate-300">
                Unsaved changes — nothing is served until you save.
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
          onCancel={() => setEditing(null)}
          onDone={(agent) => {
            setComposed((list) =>
              editing.index === null
                ? [...list, agent]
                : list.map((a, j) => (j === editing.index ? agent : a)),
            );
            setEditing(null);
          }}
        />
      ) : null}
    </div>
  );
}

function Stat({ label, value, dot }: { label: string; value: number; dot?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900">
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

function AgentCard({ card, onToggle }: { card: Card; onToggle?: (() => void) | undefined }) {
  const groups = GUARD_ORDER.map((g) => ({
    guard: g,
    tools: card.tools.filter((t) => t.guard === g),
  })).filter((g) => g.tools.length > 0);

  return (
    <li
      className={`flex flex-col rounded-xl border bg-white p-4 transition-opacity dark:bg-slate-900 ${
        card.on
          ? "border-slate-200 dark:border-slate-800"
          : "border-dashed border-slate-300 opacity-60 dark:border-slate-700"
      }`}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold text-slate-900 dark:text-slate-100">
              {card.name}
            </span>
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300">
              {sourceLabel[card.source] ?? card.source}
            </span>
            {!card.on ? (
              <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
                off
              </span>
            ) : null}
          </div>
          {card.description ? (
            <p
              className="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400"
              title={card.description}
            >
              {card.description}
            </p>
          ) : null}
        </div>
        {onToggle && card.switchKey ? (
          <button
            type="button"
            role="switch"
            aria-checked={card.on}
            aria-label={`Enable ${card.switchKey}`}
            onClick={onToggle}
            className={`relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:outline-none dark:focus-visible:ring-offset-slate-900 ${
              card.on ? "bg-brand-600" : "bg-slate-300 dark:bg-slate-700"
            }`}
          >
            <span
              className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${
                card.on ? "left-[18px]" : "left-0.5"
              }`}
            />
          </button>
        ) : null}
      </div>

      <div className="mt-3 flex flex-col gap-2.5 border-t border-slate-100 pt-3 dark:border-slate-800">
        {groups.map(({ guard, tools }) => (
          <div key={guard}>
            <p className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-slate-500 dark:text-slate-400">
              <span className={`h-1.5 w-1.5 rounded-full ${guardDot[guard]}`} />
              {guardLabel[guard]}
              <span className="text-slate-400 tabular-nums dark:text-slate-500">
                {tools.length}
              </span>
            </p>
            <ul className="flex flex-wrap gap-1" aria-label={`${card.name} ${guard} tools`}>
              {tools.map((t) => (
                <li
                  key={t.name}
                  title={`${guardTitle[t.guard]}${t.reason ? ` — ${t.reason}` : ""}`}
                  className={`rounded-md border px-1.5 py-0.5 font-mono text-[11px] ${guardChip[t.guard]}`}
                >
                  {t.name}
                </li>
              ))}
            </ul>
          </div>
        ))}
        {groups.length === 0 ? <p className="text-xs text-slate-400">No tools.</p> : null}
      </div>
    </li>
  );
}
