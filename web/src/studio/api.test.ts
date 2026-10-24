import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { API_PREFIXES } from "../api/paths";
import { SpecRefused, specErrors, StudioClient } from "./api";
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
