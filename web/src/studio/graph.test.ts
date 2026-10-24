import { describe, expect, it } from "vitest";
import {
  addNode,
  COLUMN,
  connect,
  fitTo,
  graphToSpec,
  moveNodes,
  layout,
  LEVEL,
  relayout,
  removeEdges,
  removeNodes,
  reorient,
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

  it("gives a from → to pair listed twice its own id, so deleting one keeps the other", () => {
    const spec: CompositionSpec = {
      ...RELEASE_NOTES,
      edges: [...RELEASE_NOTES.edges, { from: "research", to: "writer" }],
    };
    const g = specToGraph(spec);
    const ids = g.edges.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
    const twin = g.edges.at(-1)!;
    expect(graphToSpec(removeEdges(g, [twin.id])).edges).toEqual(RELEASE_NOTES.edges);
    // A rename keeps the twins apart.
    const renamed = updateNode(g, "research", { ...g.nodes[0]!.data.spec, id: "scout" });
    expect(new Set(renamed.edges.map((e) => e.id)).size).toBe(renamed.edges.length);
  });

  it("lays out top to bottom on a tall canvas, and extends a pipeline downwards", () => {
    const pos = layout(
      ["a", "b", "c"],
      [
        { from: "a", to: "b" },
        { from: "b", to: "c" },
      ],
      "TB",
    );
    expect(pos).toEqual({ a: { x: 0, y: 0 }, b: { x: 0, y: LEVEL }, c: { x: 0, y: 2 * LEVEL } });
    const g = specToGraph(RELEASE_NOTES, "TB");
    expect(g.direction).toBe("TB");
    expect(graphToSpec(g)).toEqual(RELEASE_NOTES);
    const added = addNode(g, newNode("llm_agent", "editor"));
    expect(added.nodes.at(-1)!.position).toEqual({ x: 0, y: 3 * LEVEL });
    const flipped = relayout(g, "LR");
    expect(flipped.direction).toBe("LR");
    expect(flipped.nodes[1]!.position).toEqual({ x: COLUMN, y: 0 });
  });

  it("redraws a node's child links when its children change", () => {
    const sup = specToGraph(TEMPLATES.find((t) => t.id === "supervisor")!.spec);
    const boss = sup.nodes.find((n) => n.data.spec.children?.length)!;
    const kept = boss.data.spec.children!.slice(0, 1);
    const next = updateNode(sup, boss.id, { ...boss.data.spec, children: kept });
    const links = next.edges.filter((e) => e.data?.child && e.source === boss.id);
    expect(links.map((e) => e.target)).toEqual(kept);
    // Spec edges are untouched, and the spec round-trips.
    expect(graphToSpec(next).edges).toEqual(graphToSpec(sup).edges);
    expect(graphToSpec(next).nodes.find((n) => n.id === boss.id)!.children).toEqual(kept);
  });

  it("lays an auto-laid-out graph out again for a new direction, never one placed by hand", () => {
    const auto = specToGraph(RELEASE_NOTES, "LR");
    const tall = reorient(auto, "TB");
    expect(tall.direction).toBe("TB");
    expect(tall.nodes[1]!.position).toEqual({ x: 0, y: LEVEL });
    expect(reorient(tall, "TB")).toBe(tall);

    // Writer dragged 144 to the right: a resize keeps every position and the
    // direction (so the handles still match), until a Tidy.
    const dragged = moveNodes(
      auto,
      auto.nodes.map((n) =>
        n.id === "writer" ? { ...n, position: { x: n.position.x + 144, y: n.position.y } } : n,
      ),
    );
    expect(dragged.handPlaced).toBe(true);
    expect(reorient(dragged, "TB")).toBe(dragged);
    const tidied = relayout(dragged, "TB");
    expect(tidied.handPlaced).toBe(false);
    expect(reorient(tidied, "LR").direction).toBe("LR");
    // An empty canvas just takes the direction.
    expect(reorient(specToGraph({ ...RELEASE_NOTES, nodes: [], edges: [] }), "TB").direction).toBe(
      "TB",
    );
  });

  it("fits a graph centred when it fits, and from its first rank when it overflows", () => {
    const opts = { pad: 32, minZoom: 0.75, maxZoom: 1 };
    // 3 nodes left to right in a wide pane: zoom 1, centred.
    expect(
      fitTo({ x: 0, y: -60, width: 800, height: 120 }, { width: 1000, height: 900 }, opts),
    ).toEqual({ x: 100, y: 450, zoom: 1 });
    // 6 nodes left to right in a 1280×800 canvas of ~400×700: clamped to 0.75,
    // and the first rank starts at the margin rather than off screen.
    const wide = fitTo(
      { x: 0, y: -60, width: 1640, height: 150 },
      { width: 400, height: 700 },
      opts,
    );
    expect(wide.zoom).toBe(0.75);
    expect(wide.x).toBe(32);
    // The other axis still fits, so it stays centred.
    expect(wide.y).toBeCloseTo(350 - 75 * 0.75 + 60 * 0.75);
    // Overflowing both ways: the top-left of the graph sits at the margin.
    const both = fitTo(
      { x: -100, y: -200, width: 3000, height: 3000 },
      { width: 400, height: 400 },
      opts,
    );
    expect(both).toEqual({ x: 32 + 75, y: 32 + 150, zoom: 0.75 });
  });
});
