import type { PendingConfirmation } from "../api/types";
import type { ChatMessage, Decision } from "./types";

/** Argument names that identify *what* an action targets, most specific first. */
const TARGET_KEYS = [
  "name",
  "container",
  "container_name",
  "container_id",
  "deployment",
  "deployment_name",
  "pod",
  "pod_name",
  "statefulset",
  "topic",
  "topic_name",
  "group",
  "group_id",
  "index",
  "index_name",
  "service",
  "node",
];

/** The argument a person would name the action by, e.g. "orders-worker". */
export function decisionTarget(args: Record<string, unknown>): string | null {
  for (const key of TARGET_KEYS) {
    const value = args[key];
    if (typeof value === "string" && value) return value;
  }
  const first = Object.values(args).find((v) => typeof v === "string" && v);
  return typeof first === "string" ? first : null;
}

/** Snapshot the pending action at the moment its button is clicked. */
export function decisionFor(word: Decision["word"], pending: PendingConfirmation | null): Decision {
  return {
    word,
    tool: pending?.tool_name ?? null,
    target: pending ? decisionTarget(pending.args) : null,
  };
}

/**
 * The decision a user message records, if it is one.
 *
 * A message sent from the card carries its snapshot. A reloaded transcript has
 * only the text, so the exact words the buttons send are recognised too —
 * that keeps a reopened conversation looking like the one that was written.
 */
export function decisionOf(m: ChatMessage): Decision | null {
  if (m.role !== "user") return null;
  if (m.decision) return m.decision;
  const word = m.text.trim().toLowerCase();
  return word === "approve" || word === "deny" ? { word, tool: null, target: null } : null;
}
