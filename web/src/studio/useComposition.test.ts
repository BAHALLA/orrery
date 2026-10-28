import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SpecRefused, VersionConflict, type StudioClient } from "./api";
import { RELEASE_NOTES } from "./templates";
import type { CompositionRecord, CompositionSpec } from "./types";
import { useComposition } from "./useComposition";

function record(version: number, versions = [version]): CompositionRecord {
  return {
    id: "c1",
    name: RELEASE_NOTES.name,
    version,
    versions,
    pattern: "pipeline",
    description: "",
    created_by: "ada",
    created_at: 1,
    updated_by: "ada",
    updated_at: 1,
    // The server fills in defaults: never equal to the client's spec.
    spec: { ...RELEASE_NOTES, description: "" },
  };
}

function fakeClient() {
  const client = {
    createComposition: vi.fn<(spec: CompositionSpec) => Promise<CompositionRecord>>(),
    updateComposition:
      vi.fn<(id: string, spec: CompositionSpec, v: number) => Promise<CompositionRecord>>(),
  };
  return { client, asClient: client as unknown as StudioClient };
}

describe("useComposition", () => {
  it("is clean right after a save, dirty after an edit, and runs the saved version", async () => {
    const { client, asClient } = fakeClient();
    client.createComposition.mockResolvedValue(record(1));
    let spec = RELEASE_NOTES;
    const { result, rerender } = renderHook(() => useComposition(asClient, spec));
    expect(result.current.dirty).toBe(true);
    await act(async () => {
      expect(await result.current.save(spec)).toEqual({ ok: true });
    });
    expect(result.current.dirty).toBe(false);
    expect(result.current.runSource).toEqual({ compositionId: "c1", version: 1 });

    spec = { ...RELEASE_NOTES, name: "renamed" };
    rerender();
    expect(result.current.dirty).toBe(true);
    expect(result.current.runSource).toBeUndefined();
  });

  it("saves a new version against the newest one known, so an old version is a restore", async () => {
    const { client, asClient } = fakeClient();
    client.updateComposition.mockResolvedValue(record(4, [1, 2, 3, 4]));
    const { result } = renderHook(() => useComposition(asClient, RELEASE_NOTES));
    act(() => result.current.adopt(record(2, [1, 2, 3]), RELEASE_NOTES));
    await act(async () => {
      await result.current.save(RELEASE_NOTES);
    });
    expect(client.updateComposition).toHaveBeenCalledWith("c1", RELEASE_NOTES, 3);
    expect(result.current.saved?.version).toBe(4);
  });

  it("reports a conflict and field errors instead of saving over them", async () => {
    const { client, asClient } = fakeClient();
    const { result } = renderHook(() => useComposition(asClient, RELEASE_NOTES));
    act(() => result.current.adopt(record(1), RELEASE_NOTES));
    client.updateComposition.mockRejectedValueOnce(new VersionConflict("changed", 5));
    let outcome: unknown;
    await act(async () => {
      outcome = await result.current.save(RELEASE_NOTES);
    });
    expect(outcome).toEqual({ ok: false, conflict: 5 });

    client.createComposition.mockRejectedValueOnce(
      new SpecRefused([{ loc: ["spec", "nodes", 0, "id"], msg: "bad", code: "invalid" }]),
    );
    await act(async () => {
      outcome = await result.current.save(RELEASE_NOTES, true);
    });
    expect(outcome).toMatchObject({ ok: false, issues: [{ loc: ["nodes", 0, "id"] }] });
    // Neither failure moved the link.
    expect(result.current.saved?.version).toBe(1);
  });
});
