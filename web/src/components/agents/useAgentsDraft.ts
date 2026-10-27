import { useEffect, useMemo, useState } from "react";
import type {
  CatalogResponse,
  CatalogUpdate,
  DeclarativeAgent,
  InventoryAgent,
  ToolGuard,
} from "../../api/types";
import { describeChanges, draftChanges } from "../agentDraft";

export type Tool = InventoryAgent["tools"][number];

/** One agent on the page: served now, switched off in the draft, or both. */
export interface Card {
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

/** The composed agent open in the dialog; `index` is null for a new one. */
export interface Editing {
  index: number | null;
  agent: DeclarativeAgent;
  specialist?: string | undefined;
}

export type Show = "all" | "on" | "off";

export const EMPTY_AGENT: DeclarativeAgent = {
  name: "",
  description: "",
  instruction: "",
  tools: [],
};

/** Order-insensitive: switching a specialist off and on again reorders the list. */
const sameDraft = (a: string[], b: DeclarativeAgent[], c: string[], d: DeclarativeAgent[]) =>
  JSON.stringify([[...a].sort(), b]) === JSON.stringify([[...c].sort(), d]);

export interface AgentsDraftOptions {
  agents: InventoryAgent[];
  catalog: CatalogResponse | null;
  /** The draft of a failed save, carried onto a newer catalog version. */
  carried: CatalogUpdate | null;
  /** Told whenever the draft gains or loses unsaved changes. */
  onDirtyChange: (dirty: boolean) => void;
  onDiscard: () => void;
  onSave: (update: CatalogUpdate) => Promise<void>;
}

export interface AgentsDraft {
  /** Whether the deployment allows editing the catalog here. */
  editable: boolean;
  /** The composed agents as saved, to mark the draft's new and edited ones. */
  savedAgents: DeclarativeAgent[];
  /** The composed agents in the draft. */
  composed: DeclarativeAgent[];
  editing: Editing | null;
  setEditing: (editing: Editing | null) => void;
  saving: boolean;
  query: string;
  setQuery: (query: string) => void;
  show: Show;
  setShow: (show: Show) => void;
  /** The query, trimmed and lowercased; empty when there is nothing to match. */
  needle: string;
  /** The draft differs from the saved catalog. */
  dirty: boolean;
  /** What the draft changes, in words; null when it only reorders. */
  changes: string | null;
  /** Every agent: the catalog's specialists first, then the rest being served. */
  cards: Card[];
  /** The cards that are on in the draft. */
  live: Card[];
  /** Tool counts over the live cards. */
  counts: { tools: number; confirm: number; destructive: number };
  /** The cards the show filter and the query leave, each with its matching tools. */
  visible: Card[];
  /** Specialists the deployment's env excludes: shown, not switchable. */
  excluded: CatalogResponse["specialists"];
  switchable: Card[];
  /** Every name a new composed agent may not take, but `except`. */
  takenNames: (except: string | null) => Set<string>;
  /** Flip one specialist in the draft. */
  toggle: (key: string) => void;
  setAll: (on: boolean) => void;
  /** Put the agent from the dialog into the draft, and close it. */
  keep: (agent: DeclarativeAgent) => void;
  remove: (index: number) => void;
  save: () => Promise<void>;
  discard: () => void;
}

/**
 * The Agents page's draft: which specialists are on and the composed agents,
 * seeded from the saved catalog (or a failed save's draft), and everything the
 * page derives from it. Saving goes through `onSave`; a successful one
 * remounts the page with the new version, which re-seeds the draft.
 */
export function useAgentsDraft({
  agents,
  catalog,
  carried,
  onDirtyChange,
  onDiscard,
  onSave,
}: AgentsDraftOptions): AgentsDraft {
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

  const toggle = (key: string) =>
    setDisabled((d) => (d.includes(key) ? d.filter((k) => k !== key) : [...d, key]));

  const keep = (agent: DeclarativeAgent) => {
    const index = editing?.index ?? null;
    setComposed((list) =>
      index === null ? [...list, agent] : list.map((a, j) => (j === index ? agent : a)),
    );
    setEditing(null);
  };

  const remove = (index: number) => setComposed((list) => list.filter((_, j) => j !== index));

  return {
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
    cards,
    live,
    counts: {
      tools: allTools.length,
      confirm: count("confirm"),
      destructive: count("destructive"),
    },
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
  };
}
