/** Schedules `cb` once; returns a function that cancels it. */
export type Scheduler = (cb: () => void) => () => void;

/**
 * One callback per animation frame, or per ~16 ms where there are no frames
 * (jsdom, a worker). A hidden tab gets no frames at all, which is fine here:
 * nothing needs painting, and a flush is forced before anything that depends
 * on the buffered text (the final message, the end of the turn).
 */
export const nextFrame: Scheduler = (cb) => {
  if (typeof requestAnimationFrame === "function") {
    const id = requestAnimationFrame(() => cb());
    return () => cancelAnimationFrame(id);
  }
  const id = setTimeout(cb, 16);
  return () => clearTimeout(id);
};

export interface FrameBatcher<T> {
  /** Buffer one item; schedules a flush if none is pending. */
  push: (item: T) => void;
  /** Deliver everything buffered now, in order (no-op when empty). */
  flush: () => void;
  /** Drop everything buffered and any scheduled flush. */
  cancel: () => void;
}

/**
 * Coalesce a burst of items into one delivery per frame.
 *
 * A streamed answer arrives as dozens of `delta` events a second, and each one
 * used to be its own state update: a reducer pass, a new conversation list, a
 * re-render of the transcript. The screen can't show more than one change per
 * frame, so the deltas between two frames are joined and applied once.
 */
export function createFrameBatcher<T>(
  onFlush: (items: T[]) => void,
  schedule: Scheduler = nextFrame,
): FrameBatcher<T> {
  let buffer: T[] = [];
  let cancelScheduled: (() => void) | null = null;

  const flush = () => {
    cancelScheduled?.();
    cancelScheduled = null;
    if (buffer.length === 0) return;
    const items = buffer;
    buffer = [];
    onFlush(items);
  };

  return {
    push(item) {
      buffer.push(item);
      if (!cancelScheduled) cancelScheduled = schedule(flush);
    },
    flush,
    cancel() {
      cancelScheduled?.();
      cancelScheduled = null;
      buffer = [];
    },
  };
}
