import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TICK_MS, useNow } from "./useNow";

afterEach(() => vi.useRealTimers());

describe("useNow", () => {
  it("ticks only while on", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    let renders = 0;
    const { result, rerender } = renderHook(
      ({ on }) => {
        renders += 1;
        return useNow(on);
      },
      { initialProps: { on: false } },
    );
    const start = result.current;
    const before = renders;
    act(() => vi.advanceTimersByTime(TICK_MS * 8));
    expect(renders).toBe(before);
    expect(result.current).toBe(start);

    rerender({ on: true });
    act(() => vi.advanceTimersByTime(TICK_MS * 4));
    expect(result.current).toBeGreaterThan(start);

    rerender({ on: false });
    const paused = renders;
    act(() => vi.advanceTimersByTime(TICK_MS * 8));
    expect(renders).toBe(paused);
  });
});
