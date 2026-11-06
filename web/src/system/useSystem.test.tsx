import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { jsonResponse, restoreGlobals } from "../test/appHarness";
import { useSystem } from "./useSystem";

const ME = {
  subject: "alice",
  role: "viewer",
  autonomy_level: "L2",
  model_provider: "gemini",
  model_name: "",
  self_test_available: false,
  mission: { title: "Lab", situation: "S", goal: "G", tasks_label: "Open", starters: [] },
};

afterEach(restoreGlobals);

describe("a refetch of /me that fails", () => {
  it.each([502, 404, 401])("keeps the earlier answer on a %i", async (status) => {
    let answer = 200;
    const fetchMock = vi.fn(async () =>
      answer === 200 ? jsonResponse(ME) : new Response("{}", { status: answer }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { result, rerender } = renderHook(({ token }) => useSystem(token), {
      initialProps: { token: "a" },
    });
    await waitFor(() => expect(result.current.me?.mission?.title).toBe("Lab"));

    answer = status;
    rerender({ token: "b" });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.meError).not.toBeNull());

    expect(result.current.me?.mission?.title).toBe("Lab");
    expect(result.current.meLoaded).toBe(true);
  });
});

describe("a first /me that fails", () => {
  it("counts a 404 as a server without /me", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 404 })),
    );
    const { result } = renderHook(() => useSystem("a"));
    await waitFor(() => expect(result.current.meLoaded).toBe(true));
    expect(result.current.me).toBeNull();
    expect(result.current.meError).toBeNull();
  });

  it("doesn't count anything else as loaded", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 502 })),
    );
    const { result } = renderHook(() => useSystem("a"));
    await waitFor(() => expect(result.current.meError).not.toBeNull());
    expect(result.current.meLoaded).toBe(false);
  });
});
