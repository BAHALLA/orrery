import { useCallback, useEffect, useRef, useState } from "react";

/** How long a deleted conversation can be brought back before it is gone. */
export const UNDO_WINDOW_MS = 5000;

export interface DeferredDelete {
  /** The conversation waiting to be deleted (hidden from the history), if any. */
  pending: { id: string; title: string } | null;
  /** Hide the row now; delete it from the store once the undo window closes. */
  schedule: (id: string, title: string) => void;
  /** Keep it: cancel the delete and show the row again. */
  undo: () => void;
  /** Don't wait out the window: delete now. */
  commit: () => void;
}

/**
 * Deleting a conversation removes it from the session store for good, and it
 * used to happen on one click of a small hover-only icon. This holds the
 * delete for a few seconds behind an Undo: the row disappears at once (so the
 * click feels done), the store is only asked after the window closes — or
 * straight away if the user dismisses the notice, deletes another one, or
 * leaves the page.
 */
export function useDeferredDelete(
  remove: (id: string) => void,
  delayMs: number = UNDO_WINDOW_MS,
): DeferredDelete {
  const [pending, setPending] = useState<{ id: string; title: string } | null>(null);
  // The id being held and its timer, outside render so a commit from a timer,
  // an unmount or a second delete all see the same one.
  const held = useRef<{ id: string; timer: ReturnType<typeof setTimeout> } | null>(null);
  const removeRef = useRef(remove);
  useEffect(() => {
    removeRef.current = remove;
  }, [remove]);

  const commit = useCallback(() => {
    const current = held.current;
    if (!current) return;
    clearTimeout(current.timer);
    held.current = null;
    setPending(null);
    removeRef.current(current.id);
  }, []);

  const schedule = useCallback(
    (id: string, title: string) => {
      commit(); // one undo at a time: an earlier delete goes through now
      held.current = { id, timer: setTimeout(commit, delayMs) };
      setPending({ id, title });
    },
    [commit, delayMs],
  );

  const undo = useCallback(() => {
    const current = held.current;
    if (!current) return;
    clearTimeout(current.timer);
    held.current = null;
    setPending(null);
  }, []);

  // Leaving the console doesn't cancel a delete the user asked for.
  useEffect(() => () => commit(), [commit]);

  return { pending, schedule, undo, commit };
}
