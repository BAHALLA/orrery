import { useId, useMemo, useState } from "react";
import { CloseIcon, SearchIcon } from "../components/icons";
import { toneChip } from "../components/severity";
import { input, sectionTitle, toolTone } from "./format";
import { parseToolRef, toolLabel } from "./kinds";
import type { StudioRegistry } from "./types";
import type { Issue } from "./validate";

interface Option {
  ref: string;
  label: string;
  detail: string;
  /** Why it can't be picked (not read-only), or null. */
  blocked: string | null;
}

interface Group {
  key: string;
  title: string;
  kind: "mcp" | "a2a" | "catalog";
  options: Option[];
}

/** Everything the registry lets a node reference, grouped by server, remote and catalogue. */
function toolGroups(registry: StudioRegistry): Group[] {
  return [
    ...registry.mcp.map<Group>((server) => ({
      key: `mcp:${server.id}`,
      title: `MCP · ${server.id}`,
      kind: "mcp",
      options: server.tools.map((t) => ({
        ref: `mcp:${server.id}.${t.name}`,
        label: t.name,
        detail: t.classification === "read_only" ? "read-only" : t.classification,
        blocked:
          t.classification === "read_only"
            ? null
            : "Not classified read_only in the registry: Studio runs are read-only (L2).",
      })),
    })),
    ...registry.a2a.map<Group>((entry) => ({
      key: `a2a:${entry.id}`,
      title: `A2A · ${entry.id}`,
      kind: "a2a",
      options: entry.skills.map((skill) => {
        const card = entry.card.skills.find((s) => s.id === skill);
        return {
          ref: `a2a:${entry.id}.${skill}`,
          label: card?.name ? `${skill} — ${card.name}` : skill,
          // A card error can be a refusal or a fetch that timed out, and a run
          // fetches the card again; the server is the judge, so warn, don't block.
          detail: `${entry.effect === "acting" ? "acts: you confirm each call · " : ""}${
            entry.card.error
              ? `Card not resolved (${entry.card.error}); the run retries it.`
              : card?.description || entry.description || "remote skill"
          }`,
          blocked: null,
        };
      }),
    })),
    {
      key: "catalog",
      title: "Catalogue",
      kind: "catalog" as const,
      options: registry.catalog.map((t) => ({
        ref: `catalog:${t.ref}`,
        label: t.ref,
        detail: t.read_only ? "read-only" : "changes state",
        blocked: t.read_only ? null : "Changes state: Studio runs are read-only (L2).",
      })),
    },
  ].filter((g) => g.options.length > 0);
}

/**
 * The tools a node may call, picked from the registry. The node's current
 * references are listed first (with the problems found in them, including
 * ones the registry no longer has); below, every reachable tool grouped by
 * where it comes from, with the ones a read-only run cannot use disabled.
 */
export function ToolPicker({
  value,
  onChange,
  registry,
  issues,
  disabled,
}: {
  value: string[];
  onChange: (tools: string[]) => void;
  registry: StudioRegistry | null;
  issues: Issue[];
  disabled: boolean;
}) {
  const [query, setQuery] = useState("");
  const searchId = useId();
  const groups = useMemo(() => (registry ? toolGroups(registry) : []), [registry]);
  const q = query.trim().toLowerCase();
  const toggle = (ref: string, on: boolean) =>
    onChange(on ? [...value, ref] : value.filter((r) => r !== ref));

  return (
    <div className="flex flex-col gap-2">
      {value.length ? (
        <ul className="flex flex-col gap-1" aria-label="Selected tools">
          {value.map((ref, j) => {
            const problems = issues.filter((i) => i.loc[3] === j);
            const kind = parseToolRef(ref)?.kind ?? "catalog";
            return (
              <li key={ref} className="flex flex-col gap-0.5">
                <span className="flex items-center gap-1.5">
                  <span
                    className={`rounded px-1 py-px text-[10px] font-semibold uppercase ring-1 ring-inset ${toolTone[kind]}`}
                  >
                    {kind}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-slate-800 dark:text-slate-200">
                    {toolLabel(ref)}
                  </span>
                  <button
                    type="button"
                    disabled={disabled}
                    aria-label={`Remove ${ref}`}
                    onClick={() => toggle(ref, false)}
                    className="rounded p-0.5 text-slate-500 hover:bg-crit-bg hover:text-crit-fg focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none disabled:opacity-40"
                  >
                    <CloseIcon size={13} />
                  </button>
                </span>
                {problems.map((p) => (
                  <span key={p.msg} className="pl-1 text-[11px] text-crit-fg">
                    {p.msg}
                  </span>
                ))}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          No tools: the agent answers from its instruction and input alone.
        </p>
      )}

      {registry === null ? (
        <p className="text-xs text-slate-500 dark:text-slate-400">Loading the registry…</p>
      ) : groups.length === 0 ? (
        <p className="text-xs text-slate-500 dark:text-slate-400">
          The registry lists no MCP servers, remote agents or catalogue tools.
        </p>
      ) : (
        <div className="rounded-lg border border-slate-200 dark:border-slate-800">
          <label htmlFor={searchId} className="sr-only">
            Filter tools
          </label>
          <div className="flex items-center gap-1.5 border-b border-slate-200 px-2 dark:border-slate-800">
            <SearchIcon size={13} className="text-slate-400" />
            <input
              id={searchId}
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter tools"
              className={`${input} border-0 bg-transparent px-0 focus:ring-0 dark:bg-transparent`}
            />
          </div>
          <div className="orrery-scroll max-h-64 overflow-y-auto p-1.5">
            {groups.map((g) => {
              const options = g.options.filter(
                (o) => !q || o.ref.toLowerCase().includes(q) || o.detail.toLowerCase().includes(q),
              );
              if (!options.length) return null;
              return (
                <fieldset key={g.key} className="mb-1.5 last:mb-0">
                  <legend className={`${sectionTitle} px-1 py-1`}>{g.title}</legend>
                  {options.map((o) => {
                    const on = value.includes(o.ref);
                    const off = disabled || (o.blocked !== null && !on);
                    return (
                      <label
                        key={o.ref}
                        title={o.blocked ?? o.detail}
                        className={`flex items-start gap-2 rounded-md px-1.5 py-1 text-xs ${
                          off
                            ? "cursor-not-allowed opacity-60"
                            : "cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/60"
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={on}
                          disabled={off}
                          onChange={(e) => toggle(o.ref, e.target.checked)}
                          className="mt-0.5 accent-brand-600"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate font-mono text-slate-800 dark:text-slate-200">
                            {o.label}
                          </span>
                          <span className="line-clamp-1 text-[11px] text-slate-500 dark:text-slate-400">
                            {o.blocked ?? o.detail}
                          </span>
                        </span>
                        {o.blocked ? (
                          <span
                            className={`shrink-0 rounded px-1 text-[10px] font-semibold ${toneChip.refused}`}
                          >
                            blocked
                          </span>
                        ) : null}
                      </label>
                    );
                  })}
                </fieldset>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
