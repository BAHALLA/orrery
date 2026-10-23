import type { SpecialistInfo } from "./api/types";

/**
 * Which specialists this deployment runs, for copy and labels.
 *
 * Specialists are discovered server-side (`orrery.specialists` entry points,
 * narrowed by `ORRERY_SPECIALISTS` / `ORRERY_DISABLED_SPECIALISTS` and the
 * admin catalog), so the console can't know them at build time. When `/me`
 * carries a `specialists` list the console uses it; until then — or from a
 * server that doesn't send one — it falls back to the stock set below, which
 * is what every deployment ran before specialists became configurable.
 *
 * Display only: hiding a suggestion never decides what the coordinator may
 * reach. The server does that.
 */
export const DEFAULT_SPECIALISTS: readonly SpecialistInfo[] = [
  { key: "kafka", name: "kafka_health_agent", label: "Kafka" },
  { key: "k8s", name: "k8s_health_agent", label: "Kubernetes" },
  { key: "elasticsearch", name: "elasticsearch_agent", label: "Elasticsearch" },
  { key: "observability", name: "observability_agent", label: "Observability" },
  { key: "docker", name: "docker_agent", label: "Docker" },
  { key: "ops_journal", name: "ops_journal_agent", label: "Ops journal" },
];

/** Stock labels by key, for a server list that names a known key without one. */
const STOCK_LABELS = new Map(DEFAULT_SPECIALISTS.map((s) => [s.key, s.label]));

/** Specialists that speak to an infrastructure system (the ops journal keeps
 * notes; it isn't something a health sweep or a status chip is about). */
const NOT_A_SYSTEM = new Set(["ops_journal"]);

/** "kafka_health_agent" → "Kafka health"; "billing" → "Billing". */
export function titleCase(name: string): string {
  const words = name.replace(/_(agent|specialist|checker)$/, "").replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The label a specialist is shown by. */
export function specialistLabel(s: SpecialistInfo): string {
  return s.label || STOCK_LABELS.get(s.key) || titleCase(s.name || s.key);
}

/** The server's list when it sent one, otherwise the stock set. */
export function resolveSpecialists(
  fromServer: SpecialistInfo[] | null | undefined,
): readonly SpecialistInfo[] {
  return fromServer ?? DEFAULT_SPECIALISTS;
}

/** The specialists that monitor a system (everything but the ops journal). */
export function systemSpecialists(list: readonly SpecialistInfo[]): SpecialistInfo[] {
  return list.filter((s) => !NOT_A_SYSTEM.has(s.key));
}

/** Their labels — for "Ask about …" copy. */
export function systemLabels(list: readonly SpecialistInfo[]): string[] {
  return systemSpecialists(list).map(specialistLabel);
}

/** "A, B and C" (or "A" / "A and B"). */
export function joinLabels(labels: string[], last = "and"): string {
  if (labels.length <= 1) return labels.join("");
  return `${labels.slice(0, -1).join(", ")} ${last} ${labels[labels.length - 1]}`;
}

// The active list, for code that runs outside React (trace labels, the triage
// rollup). Set from `/me` by useSystem; read at call time.
let active: readonly SpecialistInfo[] = DEFAULT_SPECIALISTS;

export function setActiveSpecialists(list: readonly SpecialistInfo[]): void {
  active = list;
}

export function activeSpecialists(): readonly SpecialistInfo[] {
  return active;
}

/**
 * The specialist an agent name belongs to: its own agent (`kafka_health_agent`)
 * or anything named after its key (`kafka_health_checker`, the triage
 * workflow's node for the same system).
 */
export function specialistFor(
  agent: string,
  list: readonly SpecialistInfo[] = active,
): SpecialistInfo | null {
  return (
    list.find((s) => s.name === agent) ??
    list.find((s) => agent === s.key || agent.startsWith(`${s.key}_`)) ??
    null
  );
}
