import { createContext, useContext } from "react";

/**
 * Brings the Approve/Deny card into view and focuses it, or null when no card
 * is on screen for this conversation (the action was decided, or it is
 * waiting in another conversation).
 *
 * A context rather than a prop: the trace chips that use it sit several
 * memoized components deep in the transcript, and threading a callback through
 * each would re-render the whole transcript whenever it changed.
 */
export const ShowApprovalContext = createContext<(() => void) | null>(null);

export function useShowApproval(): (() => void) | null {
  return useContext(ShowApprovalContext);
}
