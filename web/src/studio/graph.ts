import type { Edge, Node, XYPosition } from "@xyflow/react";
import type { CompositionSpec, SpecEdge, SpecNode } from "./types";

/**
 * The composition spec ↔ canvas graph mapping. Pure: no React, no DOM.
 *
 * The spec is the source of truth and the only thing that leaves the browser;
 * the graph adds what a canvas needs and a spec must not carry (the server
 * refuses unknown keys): positions and React Flow's ids. So
 * `graphToSpec(specToGraph(spec))` is `spec`, key for key, and a node's spec
 * travels inside its graph node unchanged — fields of a kind this release
 * cannot run yet survive an import, an edit elsewhere and an export.
 *
 * A supervisor's `children` are drawn as edges too (`data.child`), so the
 * canvas shows who calls whom; they are written back to `children`, never to
 * `edges`.
 */

export interface StudioNodeData extends Record<string, unknown> {
  spec: SpecNode;
}
export type StudioNode = Node<StudioNodeData, "studio">;

export interface StudioEdgeData extends Record<string, unknown> {
  spec: SpecEdge;
  /** A supervisor → child link, not a spec edge. */
  child?: boolean;
}
export type StudioEdge = Edge<StudioEdgeData>;

/** Everything in a spec but its nodes and edges. */
export type SpecMeta = Omit<CompositionSpec, "nodes" | "edges">;

export interface StudioGraph {
  meta: SpecMeta;
  nodes: StudioNode[];
  edges: StudioEdge[];
}

/** Grid of the auto-layout: column (rank) width and row height, in flow units. */
export const COLUMN = 300;
export const ROW = 170;

const ID_PATTERN = /^[a-z][a-z0-9_]{0,30}$/;

export function edgeId(from: string, to: string, child = false): string {
  return `${child ? "child" : "edge"}:${from}->${to}`;
}

function toEdge(spec: SpecEdge): StudioEdge {
  return { id: edgeId(spec.from, spec.to), source: spec.from, target: spec.to, data: { spec } };
}

function childEdges(nodes: SpecNode[]): StudioEdge[] {
  return nodes.flatMap((n) =>
    (n.children ?? []).map<StudioEdge>((child) => ({
      id: edgeId(n.id, child, true),
      source: n.id,
      target: child,
      deletable: false,
      data: { spec: { from: n.id, to: child }, child: true },
    })),
  );
}

export function specToGraph(spec: CompositionSpec): StudioGraph {
  const { nodes, edges, ...meta } = spec;
  const allEdges = [...edges.map(toEdge), ...childEdges(nodes)];
  const positions = layout(
    nodes.map((n) => n.id),
    allEdges.map((e) => ({ from: e.source, to: e.target })),
  );
  return {
    meta,
    nodes: nodes.map((n) => ({
      id: n.id,
      type: "studio",
      position: positions[n.id] ?? { x: 0, y: 0 },
      data: { spec: n },
    })),
    edges: allEdges,
  };
}

/** The spec a graph stands for: nodes in canvas order, edges in drawing order. */
export function graphToSpec({ meta, nodes, edges }: StudioGraph): CompositionSpec {
  const budgets = meta.budgets
    ? Object.fromEntries(
        Object.entries(meta.budgets).filter(([, v]) => v !== null && v !== undefined),
      )
    : undefined;
  const rest: SpecMeta = { ...meta };
  delete rest.budgets;
  return {
    ...rest,
    ...(budgets && Object.keys(budgets).length ? { budgets } : {}),
    nodes: nodes.map((n) => n.data.spec),
    edges: edges
      .filter((e) => !e.data?.child)
      .map((e) => ({ ...(e.data?.spec ?? {}), from: e.source, to: e.target })),
  };
}

/**
 * A deterministic layered layout, left to right.
 *
 * Each node's column is the longest path to it from a source (back edges, the
 * ones closing a loop, are ignored), and nodes in a column are ordered by
 * where their predecessors sit, then by their order in the spec, and centred
 * on the row axis. A pipeline is a straight line; fan-outs open into a fan.
 * Small graphs only (the format caps nodes at 30), so no layout library.
 */
export function layout(
  ids: string[],
  edges: { from: string; to: string }[],
): Record<string, XYPosition> {
  const index = new Map(ids.map((id, i) => [id, i]));
  const out = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const e of edges) {
    if (index.has(e.from) && index.has(e.to) && e.from !== e.to) out.get(e.from)?.push(e.to);
  }

  // Back edges: those reaching a node still on the DFS stack.
  const back = new Set<string>();
  const state = new Map<string, "open" | "done">();
  const visit = (id: string) => {
    state.set(id, "open");
    for (const to of out.get(id) ?? []) {
      if (state.get(to) === "open") back.add(`${id}->${to}`);
      else if (!state.has(to)) visit(to);
    }
    state.set(id, "done");
  };
  const hasIncoming = new Set(edges.map((e) => e.to));
  for (const id of ids) if (!hasIncoming.has(id) && !state.has(id)) visit(id);
  for (const id of ids) if (!state.has(id)) visit(id);

  // Longest-path ranks over the remaining DAG (Kahn's order, ties by spec order).
  const forward = (id: string) => (out.get(id) ?? []).filter((to) => !back.has(`${id}->${to}`));
  const indegree = new Map(ids.map((id) => [id, 0]));
  for (const id of ids) for (const to of forward(id)) indegree.set(to, (indegree.get(to) ?? 0) + 1);
  const rank = new Map(ids.map((id) => [id, 0]));
  const preds = new Map<string, string[]>(ids.map((id) => [id, []]));
  const queue = ids.filter((id) => indegree.get(id) === 0);
  while (queue.length) {
    queue.sort((a, b) => (index.get(a) ?? 0) - (index.get(b) ?? 0));
    const id = queue.shift() as string;
    for (const to of forward(id)) {
      preds.get(to)?.push(id);
      rank.set(to, Math.max(rank.get(to) ?? 0, (rank.get(id) ?? 0) + 1));
      indegree.set(to, (indegree.get(to) ?? 0) - 1);
      if (indegree.get(to) === 0) queue.push(to);
    }
  }

  const columns: string[][] = [];
  for (const id of ids) (columns[rank.get(id) ?? 0] ??= []).push(id);
  const row = new Map<string, number>();
  const positions: Record<string, XYPosition> = {};
  columns.forEach((column, x) => {
    const weight = (id: string) => {
      const p = (preds.get(id) ?? []).map((q) => row.get(q) ?? 0);
      return p.length ? p.reduce((a, b) => a + b, 0) / p.length : (index.get(id) ?? 0);
    };
    const ordered = [...column].sort(
      (a, b) => weight(a) - weight(b) || (index.get(a) ?? 0) - (index.get(b) ?? 0),
    );
    ordered.forEach((id, i) => {
      row.set(id, i);
      positions[id] = { x: x * COLUMN, y: (i - (ordered.length - 1) / 2) * ROW };
    });
  });
  return positions;
}

/** Re-run the auto-layout over a graph, keeping everything but positions. */
export function relayout(graph: StudioGraph): StudioGraph {
  const positions = layout(
    graph.nodes.map((n) => n.id),
    graph.edges.map((e) => ({ from: e.source, to: e.target })),
  );
  return {
    ...graph,
    nodes: graph.nodes.map((n) => ({ ...n, position: positions[n.id] ?? n.position })),
  };
}

/** `base`, made a valid node id and unique among `taken`: `writer`, `writer_2`… */
export function uniqueId(base: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  let stem = base
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^[^a-z]+/, "")
    .slice(0, 26);
  if (!stem) stem = "node";
  if (!used.has(stem)) return stem;
  for (let i = 2; ; i++) {
    const candidate = `${stem}_${i}`;
    if (!used.has(candidate)) return candidate;
  }
}

export function isValidId(id: string): boolean {
  return ID_PATTERN.test(id);
}

/** Nodes with no outgoing spec edge, in canvas order. */
export function sinks(graph: StudioGraph): StudioNode[] {
  const from = new Set(graph.edges.filter((e) => !e.data?.child).map((e) => e.source));
  return graph.nodes.filter((n) => !from.has(n.id));
}

/**
 * Add a node. Dropped on the canvas it goes where it was dropped; added from
 * the keyboard (no position) it goes after the graph's single end node and is
 * wired to it, which is what extending a pipeline means.
 */
export function addNode(graph: StudioGraph, spec: SpecNode, position?: XYPosition): StudioGraph {
  const node: StudioNode = {
    id: spec.id,
    type: "studio",
    position: position ?? { x: 0, y: 0 },
    data: { spec },
  };
  if (position) return { ...graph, nodes: [...graph.nodes, node] };
  const ends = sinks(graph);
  const tail = ends.length === 1 ? ends[0] : graph.nodes.at(-1);
  node.position = tail ? { x: tail.position.x + COLUMN, y: tail.position.y } : { x: 0, y: 0 };
  const next = { ...graph, nodes: [...graph.nodes, node] };
  return ends.length === 1 && tail ? connect(next, tail.id, spec.id) : next;
}

/** Add an edge, unless it would loop on one node or already exists. */
export function connect(graph: StudioGraph, from: string, to: string): StudioGraph {
  if (from === to || graph.edges.some((e) => e.id === edgeId(from, to))) return graph;
  return { ...graph, edges: [...graph.edges, toEdge({ from, to })] };
}

/** Remove nodes (and every edge and child link touching them). */
export function removeNodes(graph: StudioGraph, ids: Iterable<string>): StudioGraph {
  const gone = new Set(ids);
  return {
    ...graph,
    nodes: graph.nodes
      .filter((n) => !gone.has(n.id))
      .map((n) =>
        n.data.spec.children?.some((c) => gone.has(c))
          ? {
              ...n,
              data: {
                ...n.data,
                spec: {
                  ...n.data.spec,
                  children: n.data.spec.children.filter((c) => !gone.has(c)),
                },
              },
            }
          : n,
      ),
    edges: graph.edges.filter((e) => !gone.has(e.source) && !gone.has(e.target)),
  };
}

export function removeEdges(graph: StudioGraph, ids: Iterable<string>): StudioGraph {
  const gone = new Set(ids);
  return { ...graph, edges: graph.edges.filter((e) => e.data?.child || !gone.has(e.id)) };
}

/** Replace one node's spec. A changed id is carried into every edge and child list. */
export function updateNode(graph: StudioGraph, id: string, spec: SpecNode): StudioGraph {
  const renamed = spec.id !== id;
  const swap = (x: string) => (renamed && x === id ? spec.id : x);
  return {
    ...graph,
    nodes: graph.nodes.map((n) => {
      if (n.id === id) return { ...n, id: spec.id, data: { ...n.data, spec } };
      if (renamed && n.data.spec.children?.includes(id)) {
        return {
          ...n,
          data: { ...n.data, spec: { ...n.data.spec, children: n.data.spec.children.map(swap) } },
        };
      }
      return n;
    }),
    edges: renamed
      ? graph.edges.map((e) => {
          if (e.source !== id && e.target !== id) return e;
          const source = swap(e.source);
          const target = swap(e.target);
          return {
            ...e,
            id: edgeId(source, target, e.data?.child),
            source,
            target,
            data: {
              ...(e.data ?? {}),
              spec: { ...(e.data?.spec ?? {}), from: source, to: target },
            },
          };
        })
      : graph.edges,
  };
}
