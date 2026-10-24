import { useEffect, useMemo, useState } from "react";
import type {
  CatalogResponse,
  CatalogUpdate,
  DeclarativeAgent,
  DeploymentInfo,
  InventoryAgent,
  ToolGuard,
} from "../api/types";
import type { SystemController } from "../system/useSystem";
import { AgentForm } from "./AgentForm";
import { ageLabel, copyName, describeChanges, draftChanges, guardCounts } from "./agentDraft";
import { GUARD_ORDER, guardChip, guardDot, guardLabel, guardTitle } from "./guards";
import { AlertIcon, PlusIcon, SearchIcon, ShieldIcon } from "./icons";

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

const pill =
  "rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300";

type Show = "all" | "on" | "off";

/**
 * What the coordinator can reach, for admins, as a full page: what is running
 * (versions, model), each agent with its tools grouped by guard level, and —
 * when the deployment allows editing — a switch per specialist and the
 * composed agents, saved together.
 *
 * Served agents come from the running tree (`/admin/agents`), so a disabled
 * specialist is absent there because it is absent from what runs. The catalog
 * adds what is *not* served: switched-off specialists (tools taken from the
 * catalog's tool list) and the ones the deployment's env excludes.
 */
export function AgentsDashboard({ system, active, onDirtyChange }: Props) {
  const { agents, deployment, agentsError, loadAgents, loadCatalog, saveCatalog } = system;
  // Only a well-formed catalog counts: an older server answers with something else.
  const catalog = system.catalog?.catalog ? system.catalog : null;
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  // The draft of a save that failed. A conflict reloads the catalog, and the new
  // version remounts the dashboard: without this the admin's edits would vanish.
  const [carried, setCarried] = useState<CatalogUpdate | null>(null);

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
          className="mx-auto max-w-6xl rounded-lg bg-crit-bg px-3 py-2 text-sm text-crit-fg"
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
      deployment={deployment}
      catalog={catalog}
      active={active}
      onDirtyChange={onDirtyChange}
      savedVersion={savedVersion}
      carried={carried}
      saveError={saveError}
      onDiscard={() => {
        setSaveError(null);
        setCarried(null);
      }}
      onSave={async (update) => {
        // Cleared first: a successful save remounts the dashboard *during*
        // saveCatalog, and a draft from an earlier failure must not seed it.
        setCarried(null);
        const error = await saveCatalog(update);
        setSaveError(error);
        if (error) setCarried(update);
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
  /** Whether it is on in the saved catalog: differs from `on` while a switch is pending. */
  savedOn: boolean;
  /** Only for agents being served now: the tree is the source of these. */
  live?: Pick<InventoryAgent, "key" | "package" | "version" | "model" | "gated"> | undefined;
}

interface Editing {
  index: number | null;
  agent: DeclarativeAgent;
  specialist?: string | undefined;
}

function Dashboard({
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
  const editable = catalog?.editing_enabled === true;
  const savedDisabled = catalog?.catalog.disabled_specialists ?? [];
  const savedAgents = catalog?.catalog.agents ?? [];
  // Only a draft written against an older version is carried onto this one.
  const carry = carried && catalog && carried.version < catalog.catalog.version ? carried : null;
  const [disabled, setDisabled] = useState<string[]>(carry?.disabled_specialists ?? savedDisabled);
  const [composed, setComposed] = useState<DeclarativeAgent[]>(carry?.agents ?? savedAgents);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState("");
  const [show, setShow] = useState<Show>("all");

  const dirty = editable && !sameDraft(disabled, composed, savedDisabled, savedAgents);
  const changes = describeChanges(draftChanges(savedDisabled, disabled, savedAgents, composed));

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
        savedOn: !(catalog?.catalog.disabled_specialists ?? []).includes(s.key),
        live,
      };
    });
    // Composed agents get their own section while editing; elsewhere they are
    // just more agents being served.
    const rest = agents
      .filter((a) => !claimed.has(a.name) && !(editable && a.source === "declarative"))
      .map<Card>((a) => ({ ...a, on: true, savedOn: true, live: a }));
    return [...fromCatalog, ...rest];
  }, [agents, catalog, disabled, editable]);

  const live = cards.filter((c) => c.on);
  const allTools = live.flatMap((c) => c.tools);
  const count = (g: ToolGuard) => allTools.filter((t) => t.guard === g).length;

  const needle = query.trim().toLowerCase();
  const visible = cards
    .filter((c) => show === "all" || (show === "on") === c.on)
    .flatMap((c) => {
      if (!needle) return [c];
      const meta = `${c.name} ${c.description} ${c.live?.package ?? ""} ${c.live?.model ?? ""}`;
      if (meta.toLowerCase().includes(needle)) return [c];
      const tools = c.tools.filter((t) => t.name.toLowerCase().includes(needle));
      return tools.length ? [{ ...c, tools }] : [];
    });

  const excluded = catalog?.specialists.filter((s) => !s.available) ?? [];
  const switchable = cards.filter((c) => c.switchKey);

  // Every name a new composed agent may not take: the server refuses a clash.
  const takenNames = (except: string | null) =>
    new Set(
      [
        ...cards.map((c) => c.name),
        // While editing, the draft decides which composed agents exist: one
        // removed in it frees its name, though the server still serves it.
        ...agents.filter((a) => !(editable && a.source === "declarative")).map((a) => a.name),
        ...(catalog?.file_agents ?? []),
        ...(catalog?.reserved_names ?? []),
        ...composed.map((a) => a.name),
      ].filter((n) => n !== except),
    );

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
    setDisabled(savedDisabled);
    setComposed(savedAgents);
    onDiscard();
  };

  const setAll = (on: boolean) =>
    setDisabled(on ? [] : switchable.map((c) => c.switchKey as string));

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
                  onRemove={() => setComposed((list) => list.filter((_, j) => j !== i))}
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

function DeploymentPanel({
  deployment,
  catalog,
}: {
  deployment: DeploymentInfo | null;
  catalog: CatalogResponse | null;
}) {
  if (!deployment && !catalog) return null;
  const rows: [string, string, (string | undefined)?][] = [];
  if (deployment) {
    rows.push(["Orrery", deployment.orrery_version || "unknown"]);
    rows.push(["Model", deployment.model || deployment.model_provider, deployment.model_provider]);
    rows.push(["Autonomy", deployment.autonomy_level ?? "not set"]);
    rows.push(["ADK", deployment.adk_version || "unknown"]);
    rows.push(["Python", deployment.python_version]);
    rows.push([
      "Up for",
      ageLabel(deployment.started_at).replace(" ago", ""),
      new Date(deployment.started_at * 1000).toLocaleString(),
    ]);
    if (deployment.enabled_specialists || deployment.disabled_specialists) {
      rows.push([
        "Selected by env",
        [
          deployment.enabled_specialists ? `only ${deployment.enabled_specialists}` : "",
          deployment.disabled_specialists ? `not ${deployment.disabled_specialists}` : "",
        ]
          .filter(Boolean)
          .join(" · "),
      ]);
    }
  }
  if (catalog) {
    const c = catalog.catalog;
    rows.push([
      "Catalog",
      `v${c.version}${c.updated_by ? ` · ${c.updated_by}` : ""}${c.updated_at ? ` · ${ageLabel(c.updated_at)}` : ""}`,
      c.updated_at ? new Date(c.updated_at * 1000).toLocaleString() : undefined,
    ]);
    if (catalog.applied_version !== c.version) {
      rows.push(["Serving", `v${catalog.applied_version} (catching up)`]);
    }
  }

  return (
    <section
      aria-label="Deployment"
      className="rounded-xl border border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900"
    >
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
        {rows.map(([label, value, title]) => (
          <div key={label} className="min-w-0">
            <dt className="text-[11px] font-medium tracking-wide text-slate-500 uppercase dark:text-slate-400">
              {label}
            </dt>
            <dd
              className="truncate font-mono text-[13px] text-slate-800 dark:text-slate-100"
              title={title ?? value}
            >
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
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

function AgentCard({
  card,
  onToggle,
  onCompose,
}: {
  card: Card;
  onToggle?: (() => void) | undefined;
  onCompose?: (() => void) | undefined;
}) {
  const [expanded, setExpanded] = useState(false);
  const groups = GUARD_ORDER.map((g) => ({
    guard: g,
    tools: card.tools.filter((t) => t.guard === g),
  })).filter((g) => g.tools.length > 0);
  const guarded = card.tools.some((t) => t.guard !== "read");
  const pending = card.on !== card.savedOn;
  const meta = card.live;

  return (
    <li
      className={`flex flex-col rounded-xl border bg-white p-4 transition-opacity dark:bg-slate-900 ${
        pending
          ? "border-brand-400 ring-1 ring-brand-400/40"
          : card.on
            ? "border-slate-200 dark:border-slate-800"
            : "border-dashed border-slate-300 dark:border-slate-700"
      } ${card.on ? "" : "opacity-70"}`}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold text-slate-900 dark:text-slate-100">
              {card.name}
            </span>
            <span className={pill}>{sourceLabel[card.source] ?? card.source}</span>
            {meta?.version ? (
              <span
                className={pill}
                title={meta.package ? `${meta.package} ${meta.version}` : undefined}
              >
                v{meta.version}
              </span>
            ) : null}
            {pending ? (
              <span className="rounded-full bg-brand-50 px-2 py-0.5 text-[11px] font-medium text-brand-700 dark:bg-brand-900/40 dark:text-brand-300">
                {card.on ? "turns on when saved" : "turns off when saved"}
              </span>
            ) : !card.on ? (
              <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
                off
              </span>
            ) : null}
          </div>
          {card.description ? (
            <p
              className={`mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400 ${expanded ? "" : "line-clamp-2"}`}
              title={expanded ? undefined : card.description}
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

      {meta && (meta.model || meta.package || meta.key || (guarded && meta.gated !== undefined)) ? (
        <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500 dark:text-slate-400">
          {meta.key ? <Meta label="key" value={meta.key} /> : null}
          {meta.package ? <Meta label="package" value={meta.package} /> : null}
          {meta.model ? <Meta label="model" value={meta.model} /> : null}
          {guarded && meta.gated !== undefined ? (
            <div className="inline-flex items-center gap-1">
              <dt className="sr-only">gate</dt>
              {meta.gated ? (
                <dd className="inline-flex items-center gap-1">
                  <ShieldIcon size={12} className="text-ok-fg" />
                  approval gate on
                </dd>
              ) : (
                <dd className="inline-flex items-center gap-1 text-crit-fg">
                  <AlertIcon size={12} />
                  no approval gate
                </dd>
              )}
            </div>
          ) : null}
        </dl>
      ) : null}

      <div className="mt-3 flex flex-col gap-2.5 border-t border-slate-100 pt-3 dark:border-slate-800">
        {groups.map(({ guard, tools }) => (
          <div key={guard}>
            <p className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-slate-500 dark:text-slate-400">
              <span className={`h-1.5 w-1.5 rounded-full ${guardDot[guard]}`} />
              {guardLabel[guard]}
              <span className="text-slate-500 tabular-nums dark:text-slate-400">
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
        {groups.length === 0 ? <p className="text-xs text-slate-500">No tools.</p> : null}
      </div>

      {card.description.length > 140 || onCompose ? (
        <div className="mt-3 flex items-center gap-3 text-xs">
          {card.description.length > 140 ? (
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => setExpanded((e) => !e)}
              className="font-medium text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
            >
              {expanded ? "Less" : "Full description"}
            </button>
          ) : null}
          {onCompose ? (
            <button
              type="button"
              onClick={onCompose}
              className="ml-auto font-medium text-brand-600 hover:underline dark:text-brand-400"
            >
              Compose from these tools
            </button>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="inline-flex min-w-0 items-center gap-1">
      <dt>{label}</dt>
      <dd className="truncate font-mono text-slate-700 dark:text-slate-200" title={value}>
        {value}
      </dd>
    </div>
  );
}

function ComposedCard({
  agent,
  saved,
  tools,
  onEdit,
  onDuplicate,
  onRemove,
}: {
  agent: DeclarativeAgent;
  saved: DeclarativeAgent | undefined;
  tools: CatalogResponse["tools"];
  onEdit: () => void;
  onDuplicate: () => void;
  onRemove: () => void;
}) {
  const counts = guardCounts(agent.tools, tools);
  const state = !saved ? "new" : JSON.stringify(saved) !== JSON.stringify(agent) ? "edited" : null;
  const SHOWN = 6;

  return (
    <li
      className={`flex flex-col rounded-xl border bg-white p-4 dark:bg-slate-900 ${
        state
          ? "border-brand-400 ring-1 ring-brand-400/40"
          : "border-slate-200 dark:border-slate-800"
      }`}
    >
      <div className="flex items-center gap-2">
        <p className="font-mono text-sm font-medium text-slate-900 dark:text-slate-100">
          {agent.name}
        </p>
        {state ? (
          <span className="rounded-full bg-brand-50 px-2 py-0.5 text-[11px] font-medium text-brand-700 dark:bg-brand-900/40 dark:text-brand-300">
            {state} · unsaved
          </span>
        ) : null}
      </div>
      <p
        className="mt-1 line-clamp-2 text-xs text-slate-500 dark:text-slate-400"
        title={agent.description}
      >
        {agent.description}
      </p>
      <ul className="mt-2 flex flex-wrap gap-1" aria-label={`${agent.name} tools`}>
        {agent.tools.slice(0, SHOWN).map((ref) => (
          <li
            key={ref}
            className="rounded-md border border-slate-200 px-1.5 py-0.5 font-mono text-[11px] text-slate-600 dark:border-slate-700 dark:text-slate-300"
          >
            {ref}
          </li>
        ))}
        {agent.tools.length > SHOWN ? (
          <li className="px-1 py-0.5 text-[11px] text-slate-500">
            +{agent.tools.length - SHOWN} more
          </li>
        ) : null}
      </ul>
      <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
        <span className="inline-flex items-center gap-2 text-slate-500">
          {agent.tools.length} tool{agent.tools.length === 1 ? "" : "s"}
          {counts.confirm ? (
            <span className="inline-flex items-center gap-1" title="Need approval">
              <span className={`h-1.5 w-1.5 rounded-full ${guardDot.confirm}`} />
              {counts.confirm}
            </span>
          ) : null}
          {counts.destructive ? (
            <span className="inline-flex items-center gap-1" title="Destructive">
              <span className={`h-1.5 w-1.5 rounded-full ${guardDot.destructive}`} />
              {counts.destructive}
            </span>
          ) : null}
        </span>
        <button
          type="button"
          className="ml-auto font-medium text-brand-600 hover:underline dark:text-brand-400"
          onClick={onEdit}
        >
          Edit
        </button>
        <button
          type="button"
          aria-label={`Duplicate agent ${agent.name}`}
          className="font-medium text-slate-600 hover:underline dark:text-slate-300"
          onClick={onDuplicate}
        >
          Duplicate
        </button>
        <button
          type="button"
          aria-label={`Remove agent ${agent.name}`}
          className="font-medium text-crit-fg hover:underline"
          onClick={onRemove}
        >
          Remove
        </button>
      </div>
    </li>
  );
}
