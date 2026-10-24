import { describe, expect, it } from "vitest";
import { releaseNotesRun } from "./fixtures/events";
import { compareRuns, MemoryHistory, type RunRecord } from "./history";
import { initialRun, reduceRun } from "./runState";
import { RELEASE_NOTES } from "./templates";

const record = (runId: string): RunRecord => ({
  runId,
  spec: RELEASE_NOTES,
  input: "kafka",
  startedAt: 0,
  view: initialRun(runId),
});

describe("MemoryHistory", () => {
  it("puts new runs first and updates a run in place", () => {
    let h = new MemoryHistory().put(record("a")).put(record("b"));
    expect(h.list().map((r) => r.runId)).toEqual(["b", "a"]);
    h = h.put({ ...record("a"), input: "changed" });
    expect(h.list().map((r) => [r.runId, r.input])).toEqual([
      ["b", "kafka"],
      ["a", "changed"],
    ]);
  });

  it("keeps the newest runs past its cap", () => {
    let h = new MemoryHistory([], 2);
    for (const id of ["a", "b", "c"]) h = h.put(record(id));
    expect(h.list().map((r) => r.runId)).toEqual(["c", "b"]);
  });
});

describe("compareRuns", () => {
  it("lines up totals and each node's tokens and latency, with B − A", () => {
    const a = releaseNotesRun("a").reduce(reduceRun, initialRun("a"));
    const slower = releaseNotesRun("b").map((e) =>
      e.type === "node.exit" && e.node_id === "writer"
        ? { ...e, elapsed_ms: 2400, tokens: { input: 500, output: 200, total: 700 } }
        : e,
    );
    const b = slower.reduce(reduceRun, initialRun("b"));
    const { totals, nodes } = compareRuns(a, b);
    expect(totals[0]).toMatchObject({ label: "Tokens", a: 2500, b: 2500, delta: 0 });
    expect(nodes.find((r) => r.label === "writer · latency")).toMatchObject({
      a: 1400,
      b: 2400,
      delta: 1000,
    });
    expect(nodes.find((r) => r.label === "writer · tokens")).toMatchObject({
      a: 500,
      b: 700,
      delta: 200,
    });
  });

  it("shows a node only one run had, with no delta", () => {
    const a = releaseNotesRun("a").reduce(reduceRun, initialRun("a"));
    const b = releaseNotesRun("b").slice(0, 4).reduce(reduceRun, initialRun("b"));
    const critic = compareRuns(a, b).nodes.find((r) => r.label === "critic · latency");
    expect(critic).toMatchObject({ a: 1200, b: null, delta: null });
  });
});
