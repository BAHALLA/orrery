import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { API_PREFIXES } from "../api/paths";
import { SpecRefused, specErrors, StudioClient, VersionConflict } from "./api";
import { RELEASE_NOTES } from "./templates";
import type { StudioEvent } from "./types";

afterEach(() => {
  vi.unstubAllGlobals();
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("StudioClient", () => {
  it("requests only proxied paths, with the bearer token", async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL) =>
      String(url).endsWith("/events")
        ? new Response("", { headers: { "Content-Type": "text/event-stream" } })
        : json({ run_id: "r" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new StudioClient("tok");
    await Promise.all([
      client.schema(),
      client.registry(),
      client.registry(true),
      client.startRun(RELEASE_NOTES, "x"),
      client.run("r"),
      client.cancel("r"),
      client.events("r", { after: 0, onEvent: () => {} }),
    ]);
    for (const [url, init] of fetchMock.mock.calls as unknown as [string, RequestInit][]) {
      const path = new URL(url, "http://x").pathname;
      expect(API_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`))).toBe(true);
      expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
    }
    expect(fetchMock.mock.calls.map(([u]) => String(u))).toContain("/studio/registry?resolve=true");
  });

  it("turns a 422 into the server's field errors", async () => {
    const errors = [
      { loc: ["spec", "nodes", 1, "tools", 0], msg: "not registered", code: "unknown_tool" },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: { errors } }, 422)),
    );
    const err = await new StudioClient("t").startRun(RELEASE_NOTES, "x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(SpecRefused);
    expect((err as SpecRefused).errors).toEqual(errors);
  });

  it("keeps any other failure an ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "Too many runs in flight" }, 429)),
    );
    const err = await new StudioClient("t").startRun(RELEASE_NOTES, "x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe("Too many runs in flight");
  });

  it("resumes a stream with Last-Event-ID and reports whether it reached run.end", async () => {
    const frames = [
      'id: 3\nevent: node.enter\ndata: {"seq":3,"run_id":"r","ts":1,"type":"node.enter","node_id":"a","kind":"llm_agent","model":"m"}\n\n',
      ": keepalive\n\n",
      "data: not json\n\n",
    ];
    const fetchMock = vi.fn(
      async () =>
        new Response(frames.join(""), { headers: { "Content-Type": "text/event-stream" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const seen: StudioEvent[] = [];
    const result = await new StudioClient("t").events("r", {
      after: 2,
      onEvent: (e) => seen.push(e),
    });
    expect(result).toBe("dropped");
    expect(seen.map((e) => e.seq)).toEqual([3]);
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>)["Last-Event-ID"]).toBe("2");
  });
});

describe("specErrors", () => {
  it("reads the Studio's 422 body and nothing else", () => {
    expect(specErrors({ detail: { errors: [{ loc: ["a", 1], msg: "m" }] } })).toEqual([
      { loc: ["a", 1], msg: "m", code: "invalid" },
    ]);
    expect(specErrors({ detail: [{ loc: ["body"], msg: "m" }] })).toBeNull();
    expect(specErrors(null)).toBeNull();
  });
});

describe("StudioClient compositions and saved runs", () => {
  it("maps a versioned write's 409 to VersionConflict, with the current version", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({ detail: { message: "the composition changed", current_version: 4 } }, 409),
      ),
    );
    const err = await new StudioClient("t")
      .updateComposition("c1", RELEASE_NOTES, 2)
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(VersionConflict);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as VersionConflict).currentVersion).toBe(4);
  });

  it("keeps a 409 with a plain message an ApiError, not a version conflict", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "on another replica" }, 409)),
    );
    const err = await new StudioClient("t").cancel("r").catch((e: unknown) => e);
    expect(err).not.toBeInstanceOf(VersionConflict);
    expect((err as ApiError).message).toBe("on another replica");
  });

  it("runs a saved version by id, and lists runs filtered by composition", async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL) =>
      String(url).includes("/studio/runs?") ? json({ runs: [] }) : json({ run_id: "r" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new StudioClient("t");
    await client.startRun({ compositionId: "c1", version: 3 }, "go");
    await client.runs({ compositionId: "c1", limit: 10 });
    const [start, list] = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(JSON.parse(String(start[1].body))).toEqual({
      composition_id: "c1",
      version: 3,
      input: "go",
    });
    expect(list[0]).toBe("/studio/runs?limit=10&composition_id=c1");
  });

  it("deletes with the listed version and resolves on a 204", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(new StudioClient("t").deleteComposition("a/b", 2)).resolves.toBeUndefined();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/studio/compositions/a%2Fb?version=2");
    expect(init.method).toBe("DELETE");
  });
});
