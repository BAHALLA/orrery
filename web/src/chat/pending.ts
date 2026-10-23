import type { PendingConfirmation } from "../api/types";

/**
 * Where the caller's pending action belongs relative to the conversation on
 * screen.
 *
 * `GET /confirmations/pending` answers per *user*, so the same pending action
 * comes back whichever conversation is open. A server that says which session
 * it is waiting in lets the console show the card only there, and point to it
 * from everywhere else. A server that doesn't (no `session_id`) gets the
 * earlier behaviour: the card shows in whichever stored conversation is open.
 *
 * - `here`: render the Approve/Deny card.
 * - `elsewhere`: render a link to the conversation it is waiting in.
 * - `none`: nothing is pending.
 */
export type PendingPlacement = "here" | "elsewhere" | "none";

export function pendingPlacement(
  pending: PendingConfirmation | null,
  activeSessionId: string | null,
): PendingPlacement {
  if (!pending) return "none";
  // An older server: shown in any stored conversation, as before (a draft
  // never fetched it then, so it stays quiet there now too).
  if (pending.session_id == null) return activeSessionId ? "here" : "none";
  return pending.session_id === activeSessionId ? "here" : "elsewhere";
}
