import type { CatalogResponse, DeclarativeAgent, ToolGuard } from "../api/types";

/** The server's rule for a declarative agent's name (agent/declarative.py `_NAME`). */
export const AGENT_NAME = /^[a-z][a-z0-9_]{1,40}$/;

/** Why *name* can't be used, or null when it can. *taken* holds every other agent's name. */
export function nameProblem(name: string, taken: ReadonlySet<string>): string | null {
  if (!name) return "Give the agent a name.";
  if (!AGENT_NAME.test(name)) {
    return "Lowercase letters, digits and underscores, starting with a letter (2–41 characters).";
  }
  if (taken.has(name)) return `${name} is already taken.`;
  return null;
}

/** `name_copy`, `name_copy_2`, ...: the first one no agent has, within the name limit. */
export function copyName(name: string, taken: ReadonlySet<string>): string {
  // Trim the original, not the result, so the suffix survives the 41-character limit.
  const fit = (suffix: string) => `${name.slice(0, 41 - suffix.length)}${suffix}`;
  if (!taken.has(fit("_copy"))) return fit("_copy");
  for (let n = 2; ; n++) {
    const next = fit(`_copy_${n}`);
    if (!taken.has(next)) return next;
  }
}

export interface DraftChanges {
  turnedOn: string[];
  turnedOff: string[];
  added: string[];
  edited: string[];
  removed: string[];
}

const same = (a: DeclarativeAgent, b: DeclarativeAgent) => JSON.stringify(a) === JSON.stringify(b);

/** What saving the draft would change, in words an admin checks before saving. */
export function draftChanges(
  savedDisabled: readonly string[],
  disabled: readonly string[],
  savedAgents: readonly DeclarativeAgent[],
  agents: readonly DeclarativeAgent[],
): DraftChanges {
  const before = new Map(savedAgents.map((a) => [a.name, a]));
  const after = new Set(agents.map((a) => a.name));
  return {
    turnedOn: savedDisabled.filter((k) => !disabled.includes(k)),
    turnedOff: disabled.filter((k) => !savedDisabled.includes(k)),
    added: agents.filter((a) => !before.has(a.name)).map((a) => a.name),
    edited: agents
      .filter((a) => {
        const old = before.get(a.name);
        return old !== undefined && !same(old, a);
      })
      .map((a) => a.name),
    removed: savedAgents.filter((a) => !after.has(a.name)).map((a) => a.name),
  };
}

/** One sentence for the save bar, or null when nothing changes. */
export function describeChanges(c: DraftChanges): string | null {
  const parts: string[] = [];
  const list = (names: string[]) => names.join(", ");
  if (c.turnedOn.length) parts.push(`turn on ${list(c.turnedOn)}`);
  if (c.turnedOff.length) parts.push(`turn off ${list(c.turnedOff)}`);
  if (c.added.length) parts.push(`add ${list(c.added)}`);
  if (c.edited.length) parts.push(`update ${list(c.edited)}`);
  if (c.removed.length) parts.push(`remove ${list(c.removed)}`);
  if (!parts.length) return null;
  const sentence = parts.join(" · ");
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

type CatalogTool = CatalogResponse["tools"][number];

/** How many of *refs* need approval, and how many are destructive. */
export function guardCounts(
  refs: readonly string[],
  tools: readonly CatalogTool[],
): Record<Exclude<ToolGuard, "dynamic">, number> {
  const byRef = new Map(tools.map((t) => [t.ref, t.guard]));
  const counts = { read: 0, confirm: 0, destructive: 0 };
  for (const ref of refs) counts[byRef.get(ref) ?? "read"] += 1;
  return counts;
}

/** Catalog tools grouped by specialist, in the catalog's own order. */
export function toolsBySpecialist(tools: readonly CatalogTool[]): [string, CatalogTool[]][] {
  const groups = new Map<string, CatalogTool[]>();
  for (const t of tools) groups.set(t.specialist, [...(groups.get(t.specialist) ?? []), t]);
  return [...groups];
}

/** A short age: "just now", "5m ago", "3h ago", "2d ago". */
export function ageLabel(epochSeconds: number, now: number = Date.now()): string {
  const secs = Math.max(0, Math.round(now / 1000 - epochSeconds));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

/** A starting brief for a new agent: the shape the specialists' own prompts use. */
export const INSTRUCTION_TEMPLATE = `You are a focused assistant for <what this agent is for>.

## When you are asked
- <the question this agent answers>

## How to work
1. Start with the read-only tools to see the current state.
2. Report numbers and names exactly as the tools return them.
3. Only change something when the user asked for it; say what you will change first.`;
