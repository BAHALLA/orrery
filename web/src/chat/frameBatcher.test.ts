import { describe, expect, it, vi } from "vitest";
import { createFrameBatcher, type Scheduler } from "./frameBatcher";

/** A scheduler the test fires by hand, standing in for requestAnimationFrame. */
function manualFrames() {
  let pending: (() => void) | null = null;
  const schedule: Scheduler = vi.fn((cb: () => void) => {
    pending = cb;
    return () => {
      pending = null;
    };
  });
  return {
    schedule,
    frame: () => {
      const cb = pending;
      pending = null;
      cb?.();
    },
  };
}

describe("createFrameBatcher", () => {
  it("delivers a burst of deltas as one update per frame, in order", () => {
    const frames = manualFrames();
    const onFlush = vi.fn();
    const batcher = createFrameBatcher<string>(onFlush, frames.schedule);

    batcher.push("Lag ");
    batcher.push("is ");
    batcher.push("flat.");
    expect(onFlush).not.toHaveBeenCalled();
    // One frame scheduled for the whole burst, not one per delta.
    expect(frames.schedule).toHaveBeenCalledTimes(1);

    frames.frame();
    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush).toHaveBeenCalledWith(["Lag ", "is ", "flat."]);

    batcher.push("Next");
    expect(frames.schedule).toHaveBeenCalledTimes(2);
    frames.frame();
    expect(onFlush).toHaveBeenLastCalledWith(["Next"]);
  });

  it("flushes early on demand and cancels the scheduled frame", () => {
    const frames = manualFrames();
    const onFlush = vi.fn();
    const batcher = createFrameBatcher<string>(onFlush, frames.schedule);

    batcher.push("a");
    batcher.flush();
    expect(onFlush).toHaveBeenCalledWith(["a"]);
    frames.frame(); // the cancelled frame delivers nothing twice
    expect(onFlush).toHaveBeenCalledTimes(1);
    batcher.flush(); // empty: no call
    expect(onFlush).toHaveBeenCalledTimes(1);
  });

  it("drops buffered items on cancel", () => {
    const frames = manualFrames();
    const onFlush = vi.fn();
    const batcher = createFrameBatcher<string>(onFlush, frames.schedule);
    batcher.push("a");
    batcher.cancel();
    frames.frame();
    batcher.flush();
    expect(onFlush).not.toHaveBeenCalled();
  });
});
