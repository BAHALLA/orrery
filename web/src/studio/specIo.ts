import { KINDS } from "./kinds";
import type { CompositionSpec, SpecEdge, SpecNode } from "./types";

/** Export and import of a composition spec as JSON. Pure. */

export function specJson(spec: CompositionSpec): string {
  return `${JSON.stringify(spec, null, 2)}\n`;
}

/** `release-notes.spec.json`, from the spec's name. */
export function specFileName(spec: CompositionSpec): string {
  const stem = spec.name.replace(/[^a-z0-9_-]+/gi, "-").replace(/^-+|-+$/g, "") || "composition";
  return `${stem}.spec.json`;
}

/**
 * Expand the console's shorthand: a `remote_a2a` node becomes the
 * `llm_agent` it stands for (one `a2a:` tool and an instruction to forward),
 * which is what the server runs (AEP-031 §1).
 */
export function normalizeSpec(spec: CompositionSpec): { spec: CompositionSpec; notes: string[] } {
  const notes: string[] = [];
  const nodes = spec.nodes.map<SpecNode>((node) => {
    if (node.kind !== "remote_a2a") return node;
    notes.push(`"${node.id}" (remote_a2a) became an llm_agent holding a2a:${node.remote ?? "?"}.`);
    const { remote, instruction, ...rest } = node;
    return {
      ...rest,
      kind: "llm_agent",
      instruction:
        instruction ||
        "Send your input, unchanged, to the remote agent's tool, then answer with exactly what it returns.",
      tools: remote ? [`a2a:${remote}`] : [],
    };
  });
  return { spec: { ...spec, nodes }, notes };
}

export type ImportResult =
  { ok: true; spec: CompositionSpec; notes: string[] } | { ok: false; error: string };

/**
 * Read a pasted or uploaded spec. Only the shape the canvas needs is checked
 * here (nodes with ids and kinds, edges with ends); everything else is the
 * live validation's job once it is on the canvas, so a spec with problems can
 * still be loaded and fixed.
 */
export function parseSpec(text: string): ImportResult {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    return { ok: false, error: `Not JSON: ${err instanceof Error ? err.message : String(err)}` };
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, error: "A composition is a JSON object." };
  }
  const obj = raw as Record<string, unknown>;
  // A whole POST /studio/runs body is accepted too.
  const spec = (obj.spec && typeof obj.spec === "object" ? obj.spec : obj) as Record<
    string,
    unknown
  >;
  if (!Array.isArray(spec.nodes))
    return { ok: false, error: "The composition has no `nodes` list." };
  const edges = spec.edges ?? [];
  if (!Array.isArray(edges)) return { ok: false, error: "`edges` must be a list." };
  const ids = new Set<string>();
  for (const [i, node] of spec.nodes.entries()) {
    if (!node || typeof node !== "object")
      return { ok: false, error: `nodes[${i}] is not an object.` };
    const { id, kind } = node as Record<string, unknown>;
    if (typeof id !== "string" || !id) return { ok: false, error: `nodes[${i}] has no id.` };
    if (typeof kind !== "string" || !(kind in KINDS)) {
      return {
        ok: false,
        error: `nodes[${i}] ("${id}") has an unknown kind ${JSON.stringify(kind)}.`,
      };
    }
    if (ids.has(id))
      return { ok: false, error: `Two nodes are called "${id}": the canvas needs unique ids.` };
    ids.add(id);
  }
  for (const [j, edge] of edges.entries()) {
    const { from, to } = (edge ?? {}) as Record<string, unknown>;
    if (typeof from !== "string" || typeof to !== "string") {
      return { ok: false, error: `edges[${j}] needs "from" and "to".` };
    }
  }
  const full = {
    spec_version: 1,
    name: "imported",
    ...spec,
    nodes: spec.nodes as SpecNode[],
    edges: edges as SpecEdge[],
  } as CompositionSpec;
  const { spec: normal, notes } = normalizeSpec(full);
  const dangling = normal.edges.filter((e) => !ids.has(e.from) || !ids.has(e.to));
  if (dangling.length)
    notes.push(`${dangling.length} edge(s) point at nodes that are not in the spec.`);
  return { ok: true, spec: normal, notes };
}
