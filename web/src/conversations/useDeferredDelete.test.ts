import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDeferredDelete } from "./useDeferredDelete";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useDeferredDelete", () => {
  it("deletes only once the undo window has closed", () => {
    const remove = vi.fn();
    const { result } = renderHook(() => useDeferredDelete(remove, 5000));

    act(() => result.current.schedule("a", "A"));
    expect(result.current.pending).toEqual({ id: "a", title: "A" });
    act(() => vi.advanceTimersByTime(4999));
    expect(remove).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(1));
    expect(remove).toHaveBeenCalledWith("a");
    expect(result.current.pending).toBeNull();
  });

  it("undo keeps the conversation", () => {
    const remove = vi.fn();
    const { result } = renderHook(() => useDeferredDelete(remove, 5000));

    act(() => result.current.schedule("a", "A"));
    act(() => result.current.undo());
    act(() => vi.advanceTimersByTime(10_000));

    expect(remove).not.toHaveBeenCalled();
    expect(result.current.pending).toBeNull();
  });

  it("a second delete sends the first one through at once", () => {
    const remove = vi.fn();
    const { result } = renderHook(() => useDeferredDelete(remove, 5000));

    act(() => result.current.schedule("a", "A"));
    act(() => result.current.schedule("b", "B"));

    expect(remove).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledWith("a");
    expect(result.current.pending?.id).toBe("b");
  });

  it("leaving the page doesn't cancel the delete", () => {
    const remove = vi.fn();
    const { result, unmount } = renderHook(() => useDeferredDelete(remove, 5000));

    act(() => result.current.schedule("a", "A"));
    unmount();

    expect(remove).toHaveBeenCalledWith("a");
  });
});
