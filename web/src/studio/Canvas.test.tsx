import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useSettledDirection } from "./Canvas";

describe("useSettledDirection", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const mount = (tall: boolean | null) => {
    const onDirection = vi.fn();
    const hook = renderHook(
      ({ value }: { value: boolean | null }) => useSettledDirection(value, onDirection),
      { initialProps: { value: tall } },
    );
    return { onDirection, rerender: (value: boolean | null) => hook.rerender({ value }) };
  };

  it("reports a shape that lasts", () => {
    const { onDirection } = mount(true);
    expect(onDirection).not.toHaveBeenCalled();
    vi.advanceTimersByTime(250);
    expect(onDirection).toHaveBeenCalledExactlyOnceWith("TB");
  });

  it("ignores a shape the rail's animation only passes through", () => {
    const { onDirection, rerender } = mount(false);
    vi.advanceTimersByTime(250);
    onDirection.mockClear();

    rerender(null); // hidden
    rerender(true); // shown again, the rail still folding
    vi.advanceTimersByTime(150);
    rerender(false);
    vi.advanceTimersByTime(250);

    expect(onDirection).not.toHaveBeenCalledWith("TB");
  });

  it("reports nothing for a hidden pane", () => {
    const { onDirection, rerender } = mount(true);
    rerender(null);
    vi.advanceTimersByTime(1000);
    expect(onDirection).not.toHaveBeenCalled();
  });
});
