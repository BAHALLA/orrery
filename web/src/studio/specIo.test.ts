import { describe, expect, it } from "vitest";
import { normalizeSpec, parseSpec, specFileName, specJson } from "./specIo";
import { RELEASE_NOTES } from "./templates";

describe("export", () => {
  it("is the spec as indented JSON, named after it", () => {
    expect(JSON.parse(specJson(RELEASE_NOTES))).toEqual(RELEASE_NOTES);
    expect(specFileName(RELEASE_NOTES)).toBe("release-notes.spec.json");
  });
});

describe("import", () => {
  it("reads a spec and a whole POST /studio/runs body", () => {
    expect(parseSpec(specJson(RELEASE_NOTES))).toEqual({
      ok: true,
      spec: RELEASE_NOTES,
      notes: [],
    });
    const body = JSON.stringify({ spec: RELEASE_NOTES, input: "x" });
    expect(parseSpec(body)).toMatchObject({ ok: true, spec: RELEASE_NOTES });
  });

  it("explains what it can't load", () => {
    expect(parseSpec("{")).toMatchObject({ ok: false, error: expect.stringMatching(/^Not JSON/) });
    expect(parseSpec("[]")).toMatchObject({ ok: false });
    expect(parseSpec('{"nodes": [{"id": "a", "kind": "robot"}]}')).toMatchObject({
      ok: false,
      error: expect.stringMatching(/unknown kind/),
    });
    expect(
      parseSpec('{"nodes": [{"id": "a", "kind": "join"}, {"id": "a", "kind": "join"}]}'),
    ).toMatchObject({ ok: false, error: expect.stringMatching(/unique/) });
    expect(parseSpec('{"nodes": [], "edges": [{"from": "a"}]}')).toMatchObject({ ok: false });
  });

  it("fills spec_version and name, and notes dangling edges", () => {
    const r = parseSpec(
      '{"nodes": [{"id": "a", "kind": "llm_agent", "instruction": "x"}], "edges": [{"from": "a", "to": "b"}]}',
    );
    expect(r).toMatchObject({ ok: true, spec: { spec_version: 1, name: "imported" } });
    expect(r.ok && r.notes[0]).toMatch(/1 edge/);
  });

  it("expands the remote_a2a shorthand into the llm_agent the server runs", () => {
    const { spec, notes } = normalizeSpec({
      ...RELEASE_NOTES,
      nodes: [{ id: "writer", kind: "remote_a2a", remote: "demo-writer.draft" }],
      edges: [],
    });
    expect(spec.nodes[0]).toEqual({
      id: "writer",
      kind: "llm_agent",
      instruction: expect.stringMatching(/unchanged/),
      tools: ["a2a:demo-writer.draft"],
    });
    expect(notes).toHaveLength(1);
  });
});
