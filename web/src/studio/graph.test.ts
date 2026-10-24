import { describe, expect, it } from "vitest";
import {
  addNode,
  COLUMN,
  connect,
  graphToSpec,
  layout,
  removeEdges,
  removeNodes,
  specToGraph,
  uniqueId,
  updateNode,
} from "./graph";
import { newNode } from "./kinds";
import { RELEASE_NOTES, TEMPLATES } from "./templates";
import type { CompositionSpec } from "./types";

describe("spec ↔ graph", () => {
  it.each(TEMPLATES.map((t) => [t.id, t.spec] as const))(
    "round-trips the %s template key for key",
    (_id, spec) => {
      expect(graphToSpec(specToGraph(spec))).toEqual(spec);
    },
  );

  it("keeps fields of kinds this release cannot run yet", () => {
    const spec: CompositionSpec = {
      spec_version: 1,
      name: "later",
      pattern: "critic_loop",
      nodes: [
        {
          id: "a",
          kind: "llm_agent",
          instruction: "x",
          output: { labels: ["ok", "no"] },
          model: "m",
        },
        { id: "gate", kind: "human_approval", prompt: "Ship it?" },
      ],
      edges: [{ from: "a", to: "gate", when: "ok", max_iterations: 2, screen: true }],
    };
    expect(graphToSpec(specToGraph(spec))).toEqual(spec);
  });

  it("draws a supervisor's children as links, and writes them back as children", () => {
    const supervisor = TEMPLATES.find((t) => t.id === "supervisor")!.spec;
    const graph = specToGraph(supervisor);
    expect(graph.edges.filter((e) => e.data?.child).map((e) => e.target)).toEqual([
      "researcher",
      "writer",
    ]);
    expect(graphToSpec(graph).edges).toEqual([]);
  });

  it("puts no positions or React Flow fields in the spec", () => {
    const json = JSON.stringify(graphToSpec(specToGraph(RELEASE_NOTES)));
    expect(json).not.toMatch(/position|"type"|"data"|"source"|"target"/);
  });

  it("drops unset budgets rather than sending nulls", () => {
    const graph = specToGraph({ ...RELEASE_NOTES, budgets: { max_tokens: null, timeout_s: 30 } });
    expect(graphToSpec(graph).budgets).toEqual({ timeout_s: 30 });
    const none = specToGraph({ ...RELEASE_NOTES, budgets: { max_tokens: null } });
    expect(graphToSpec(none)).not.toHaveProperty("budgets");
  });
});

describe("layout", () => {
  it("lays a pipeline out in a straight line, one column per step", () => {
    const pos = layout(
      ["a", "b", "c"],
      [
        { from: "a", to: "b" },
        { from: "b", to: "c" },
      ],
    );
    expect(pos).toEqual({ a: { x: 0, y: 0 }, b: { x: COLUMN, y: 0 }, c: { x: 2 * COLUMN, y: 0 } });
  });

  it("fans out into a centred column, and is deterministic", () => {
    const fan = TEMPLATES.find((t) => t.id === "fan-out")!.spec;
    const ids = fan.nodes.map((n) => n.id);
    const edges = fan.edges;
    const pos = layout(ids, edges);
    expect(pos.logs!.x).toBe(COLUMN);
    expect(pos.metrics!.x).toBe(COLUMN);
    expect([pos.logs!.y, pos.metrics!.y, pos.events!.y]).toEqual([-170, 0, 170]);
    expect(pos.merge!.x).toBe(3 * COLUMN);
    expect(layout(ids, edges)).toEqual(pos);
  });

  it("ignores a loop's back edge when ranking", () => {
    const pos = layout(
      ["w", "c"],
      [
        { from: "w", to: "c" },
        { from: "c", to: "w" },
      ],
    );
    expect(pos.w!.x).toBe(0);
    expect(pos.c!.x).toBe(COLUMN);
  });
});

describe("editing", () => {
  const base = () => specToGraph(RELEASE_NOTES);

  it("appends a node after the single end node and wires it in", () => {
    const g = addNode(base(), newNode("llm_agent", "editor"));
    expect(g.edges.at(-1)).toMatchObject({ source: "critic", target: "editor" });
    expect(g.nodes.at(-1)!.position.x).toBe(g.nodes[2]!.position.x + COLUMN);
  });

  it("does not wire a dropped node", () => {
    const g = addNode(base(), newNode("llm_agent", "loose"), { x: 10, y: 10 });
    expect(g.edges).toHaveLength(2);
    expect(g.nodes.at(-1)!.position).toEqual({ x: 10, y: 10 });
  });

  it("carries a rename into edges", () => {
    const g = base();
    const writer = g.nodes[1]!.data.spec;
    const renamed = updateNode(g, "writer", { ...writer, id: "drafter" });
    expect(graphToSpec(renamed).edges).toEqual([
      { from: "research", to: "drafter" },
      { from: "drafter", to: "critic" },
    ]);
  });

  it("carries a rename into a supervisor's children", () => {
    const g = specToGraph(TEMPLATES.find((t) => t.id === "supervisor")!.spec);
    const writer = g.nodes.find((n) => n.id === "writer")!.data.spec;
    const spec = graphToSpec(updateNode(g, "writer", { ...writer, id: "author" }));
    expect(spec.nodes[0]!.children).toEqual(["researcher", "author"]);
  });

  it("removes a node with its edges and child links", () => {
    expect(graphToSpec(removeNodes(base(), ["writer"])).edges).toEqual([]);
    const sup = specToGraph(TEMPLATES.find((t) => t.id === "supervisor")!.spec);
    expect(graphToSpec(removeNodes(sup, ["writer"])).nodes[0]!.children).toEqual(["researcher"]);
  });

  it("refuses self-loops and duplicate edges, and removes edges by id", () => {
    const g = base();
    expect(connect(g, "writer", "writer")).toBe(g);
    expect(connect(g, "research", "writer")).toBe(g);
    const id = g.edges[0]!.id;
    expect(removeEdges(g, [id]).edges).toHaveLength(1);
  });

  it("makes valid, unique ids", () => {
    expect(uniqueId("agent", [])).toBe("agent");
    expect(uniqueId("agent", ["agent", "agent_2"])).toBe("agent_3");
    expect(uniqueId("Remote A2A!", [])).toBe("remote_a2a_");
    expect(uniqueId("42", [])).toBe("node");
  });
});
