import { KINDS, parseToolRef, type Capabilities } from "./kinds";
import type { CompositionSpec, JsonSchema, SpecErrorWire, StudioRegistry } from "./types";

/**
 * Live validation, and the mapping of every problem onto the canvas.
 *
 * Two sources, one shape. The client checks what it can as the admin types:
 * the published JSON Schema (`GET /studio/schema`, walked by
 * {@link validateAgainstSchema}) and the release's own rules
 * ({@link checkComposition}, a mirror of the server's `validate_spec`). The
 * server's 422 on `POST /studio/runs` is the authority, and its errors arrive
 * with the same `loc` paths, so {@link targetOf} places both on a node, an
 * edge, a composition field or the run input.
 */

export interface Issue {
  /** Path into the spec (`["nodes", 1, "tools", 0]`), or `["input"]`. */
  loc: (string | number)[];
  msg: string;
  code: string;
  source: "client" | "server";
}

export type Target =
  | { kind: "node"; nodeId: string; field: string | null }
  | { kind: "edge"; index: number; field: string | null }
  | { kind: "spec"; field: string | null }
  | { kind: "input" };

/** Server errors as issues: `loc` loses its `"spec"` prefix (the request body's). */
export function fromServer(errors: SpecErrorWire[]): Issue[] {
  return errors.map((e) => ({
    loc: e.loc[0] === "spec" ? e.loc.slice(1) : e.loc,
    msg: e.msg,
    code: e.code,
    source: "server",
  }));
}

/** Where an issue belongs, resolved against the spec it was found in. */
export function targetOf(loc: Issue["loc"], spec: CompositionSpec): Target {
  const [head, index, field] = loc;
  if (head === "input") return { kind: "input" };
  if (head === "nodes" && typeof index === "number") {
    const node = spec.nodes[index];
    if (node)
      return { kind: "node", nodeId: node.id, field: typeof field === "string" ? field : null };
  }
  if (head === "edges" && typeof index === "number") {
    return { kind: "edge", index, field: typeof field === "string" ? field : null };
  }
  if (head === "budgets")
    return { kind: "spec", field: typeof index === "string" ? index : "budgets" };
  return { kind: "spec", field: typeof head === "string" && loc.length ? head : null };
}

export interface IssueMap {
  byNode: Map<string, Issue[]>;
  byEdge: Map<number, Issue[]>;
  spec: Issue[];
  input: Issue[];
  count: number;
}

export function groupIssues(issues: Issue[], spec: CompositionSpec): IssueMap {
  const map: IssueMap = {
    byNode: new Map(),
    byEdge: new Map(),
    spec: [],
    input: [],
    count: issues.length,
  };
  for (const issue of issues) {
    const t = targetOf(issue.loc, spec);
    if (t.kind === "node") map.byNode.set(t.nodeId, [...(map.byNode.get(t.nodeId) ?? []), issue]);
    else if (t.kind === "edge")
      map.byEdge.set(t.index, [...(map.byEdge.get(t.index) ?? []), issue]);
    else if (t.kind === "input") map.input.push(issue);
    else map.spec.push(issue);
  }
  return map;
}

/** The issues for one field of a node (`instruction`, `tools`…). */
export function fieldIssues(issues: Issue[] | undefined, field: string): Issue[] {
  return (issues ?? []).filter((i) => i.loc[2] === field);
}

// ── JSON Schema (the subset Pydantic emits) ──────────────────────────

const ID_PATTERN = "^[a-z][a-z0-9_]{0,30}$";
const NAME_PATTERN = "^[a-z0-9][a-z0-9_-]{0,63}$";

function typeOf(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "number") return Number.isInteger(value) ? "integer" : "number";
  return typeof value;
}

function typeMatches(want: string, value: unknown): boolean {
  const got = typeOf(value);
  return got === want || (want === "number" && got === "integer");
}

function patternMessage(pattern: string): string {
  if (pattern === ID_PATTERN) {
    return "use lowercase letters, digits and _, starting with a letter (at most 31)";
  }
  if (pattern === NAME_PATTERN) {
    return "use lowercase letters, digits, _ and -, starting with a letter or digit (at most 64)";
  }
  return `must match ${pattern}`;
}

/**
 * Check `spec` against the published schema. Covers `$ref`, `type`, `const`,
 * `enum`, `required`, `properties`, `additionalProperties: false`, string
 * length and pattern, number bounds, array bounds and items, nullable
 * `anyOf`, and `oneOf` with a discriminator (the node kinds) — what
 * `CompositionSpec.model_json_schema()` uses. Anything else is left to the server.
 */
export function validateAgainstSchema(spec: unknown, schema: JsonSchema): Issue[] {
  const issues: Issue[] = [];
  const defs = schema.$defs ?? {};
  const add = (loc: Issue["loc"], msg: string, code = "invalid") =>
    issues.push({ loc, msg, code, source: "client" });

  const resolve = (s: JsonSchema): JsonSchema => {
    let current = s;
    for (let hops = 0; current.$ref && hops < 10; hops++) {
      const name = current.$ref.replace(/^#\/\$defs\//, "");
      current = defs[name] ?? {};
    }
    return current;
  };

  const walk = (value: unknown, raw: JsonSchema, loc: Issue["loc"]) => {
    const s = resolve(raw);
    if (s.anyOf) {
      if (value === null && s.anyOf.some((b) => resolve(b).type === "null")) return;
      const branch = s.anyOf.find((b) => {
        const r = resolve(b);
        return !r.type || typeMatches(r.type, value);
      });
      if (branch) walk(value, branch, loc);
      else add(loc, "has the wrong type");
      return;
    }
    if (s.oneOf && s.discriminator) {
      const prop = s.discriminator.propertyName;
      const tag =
        value && typeof value === "object" ? (value as Record<string, unknown>)[prop] : undefined;
      const ref = typeof tag === "string" ? s.discriminator.mapping?.[tag] : undefined;
      if (!ref) {
        add([...loc, prop], `unknown ${prop} ${JSON.stringify(tag ?? null)}`);
        return;
      }
      walk(value, { $ref: ref }, loc);
      return;
    }
    if (s.type && !typeMatches(s.type, value)) {
      add(loc, `must be ${s.type === "integer" ? "a whole number" : `a ${s.type}`}`);
      return;
    }
    if (s.const !== undefined && value !== s.const) add(loc, `must be ${JSON.stringify(s.const)}`);
    if (s.enum && !s.enum.includes(value)) {
      add(loc, `must be one of ${s.enum.map((v) => JSON.stringify(v)).join(", ")}`);
    }
    if (typeof value === "string") {
      if (s.minLength !== undefined && value.length < s.minLength) {
        add(loc, s.minLength === 1 ? "is required" : `needs at least ${s.minLength} characters`);
      }
      if (s.maxLength !== undefined && value.length > s.maxLength) {
        add(loc, `is too long (${value.length} of at most ${s.maxLength} characters)`);
      }
      if (s.pattern && value.length && !new RegExp(s.pattern, "u").test(value)) {
        add(loc, patternMessage(s.pattern));
      }
    }
    if (typeof value === "number") {
      if (s.minimum !== undefined && value < s.minimum) add(loc, `must be at least ${s.minimum}`);
      if (s.maximum !== undefined && value > s.maximum) add(loc, `must be at most ${s.maximum}`);
    }
    if (Array.isArray(value)) {
      if (s.minItems !== undefined && value.length < s.minItems) {
        add(loc, `needs at least ${s.minItems}`);
      }
      if (s.maxItems !== undefined && value.length > s.maxItems) {
        add(loc, `has ${value.length}, at most ${s.maxItems} allowed`);
      }
      if (s.items) value.forEach((item, i) => walk(item, s.items as JsonSchema, [...loc, i]));
    }
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const obj = value as Record<string, unknown>;
      for (const key of s.required ?? []) {
        if (!(key in obj)) add([...loc, key], "is required", "missing");
      }
      for (const [key, child] of Object.entries(obj)) {
        const prop = s.properties?.[key];
        if (prop) walk(child, prop, [...loc, key]);
        else if (s.additionalProperties === false) {
          add([...loc, key], `unknown key ${JSON.stringify(key)}`, "extra_forbidden");
        }
      }
    }
  };

  walk(spec, schema, []);
  return issues;
}

// ── This release's rules (mirrors studio/spec.py validate_spec) ──────

export interface CheckContext {
  caps: Capabilities;
  /** Tool references are checked only once the registry has loaded. */
  registry: StudioRegistry | null;
}

function toolError(ref: string, registry: StudioRegistry): string | null {
  const parsed = parseToolRef(ref);
  if (!parsed) {
    return `"${ref}" is not a tool reference: use mcp:<entry>.<tool>, a2a:<entry>.<skill> or catalog:<tool>`;
  }
  if (parsed.kind === "mcp") {
    const server = registry.mcp.find((s) => s.id === parsed.entry);
    if (!server) return `no MCP server "${parsed.entry}" in the registry`;
    const tool = server.tools.find((t) => t.name === parsed.name);
    if (!tool) return `MCP server "${parsed.entry}" does not list tool "${parsed.name}"`;
    if (tool.classification !== "read_only") {
      return `"${parsed.name}" is not classified read_only in the registry; Studio runs are read-only (L2)`;
    }
    return null;
  }
  if (parsed.kind === "a2a") {
    const remote = registry.a2a.find((e) => e.id === parsed.entry);
    if (!remote) return `no remote agent "${parsed.entry}" in the registry`;
    if (!remote.skills.includes(parsed.name)) {
      return `remote agent "${parsed.entry}" is not registered for skill "${parsed.name}"`;
    }
    return null;
  }
  const tool = registry.catalog.find((t) => t.ref === parsed.name);
  if (!tool) return `no catalogue tool "${parsed.name}"`;
  if (!tool.read_only) {
    return `catalogue tool "${parsed.name}" changes state; Studio runs are read-only (L2)`;
  }
  return null;
}

/** What this release refuses, field by field, before a run is even tried. */
export function checkComposition(spec: CompositionSpec, { caps, registry }: CheckContext): Issue[] {
  const issues: Issue[] = [];
  const add = (loc: Issue["loc"], msg: string, code = "invalid") =>
    issues.push({ loc, msg, code, source: "client" });
  const pattern = spec.pattern ?? "pipeline";
  if (!caps.patterns.has(pattern)) {
    add(["pattern"], `pattern "${pattern}" is not supported yet (P0b)`, "not_supported_yet");
  }

  const ids = new Map<string, number>();
  spec.nodes.forEach((node, i) => {
    if (ids.has(node.id)) add(["nodes", i, "id"], `duplicate node id "${node.id}"`);
    if (!ids.has(node.id)) ids.set(node.id, i);
    if (!caps.kinds.has(node.kind)) {
      add(
        ["nodes", i, "kind"],
        `${KINDS[node.kind]?.label ?? node.kind} nodes are coming in ${KINDS[node.kind]?.phase ?? "a later phase"}`,
        "not_supported_yet",
      );
      return;
    }
    if (node.model != null && !caps.perNodeModels) {
      add(
        ["nodes", i, "model"],
        "this server runs every node on the deployment's model",
        "not_supported_yet",
      );
    } else if (node.model != null && !caps.models.includes(node.model)) {
      add(
        ["nodes", i, "model"],
        `"${node.model}" is not one of this server's models`,
        "model_not_allowed",
      );
    }
    if (node.output != null && !caps.routing) {
      add(["nodes", i, "output"], "output labels are coming in P0b", "not_supported_yet");
    }
    const tools = node.tools ?? [];
    if (new Set(tools).size !== tools.length)
      add(["nodes", i, "tools"], "duplicate tool references");
    if (registry) {
      tools.forEach((ref, j) => {
        const why = toolError(ref, registry);
        if (why) add(["nodes", i, "tools", j], why, "unknown_tool");
      });
    }
  });

  if (pattern === "pipeline") issues.push(...checkPipeline(spec, ids, caps));
  return issues;
}

function checkPipeline(
  spec: CompositionSpec,
  ids: Map<string, number>,
  caps: Capabilities,
): Issue[] {
  const issues: Issue[] = [];
  const add = (loc: Issue["loc"], msg: string, code = "invalid") =>
    issues.push({ loc, msg, code, source: "client" });
  const succ = new Map<string, string>();
  const pred = new Map<string, string>();
  spec.edges.forEach((edge, j) => {
    for (const [end, name] of [
      ["from", edge.from],
      ["to", edge.to],
    ] as const) {
      if (!ids.has(name)) add(["edges", j, end], `no node "${name}"`);
    }
    if (!caps.routing) {
      if (edge.when != null)
        add(["edges", j, "when"], "conditional edges are coming in P0b", "not_supported_yet");
      if (edge.max_iterations != null) {
        add(["edges", j, "max_iterations"], "loops are coming in P0b", "not_supported_yet");
      }
      if (edge.screen)
        add(["edges", j, "screen"], "edge screening is coming in P0b", "not_supported_yet");
    }
    if (edge.from === edge.to) add(["edges", j], "an edge cannot loop on one node");
    else if (succ.has(edge.from)) {
      add(
        ["edges", j, "from"],
        `"${edge.from}" already has a next node: a pipeline does not branch`,
      );
    } else if (pred.has(edge.to)) {
      add(["edges", j, "to"], `"${edge.to}" already has a previous node: a pipeline does not join`);
    } else {
      succ.set(edge.from, edge.to);
      pred.set(edge.to, edge.from);
    }
  });
  if (issues.length) return issues;

  const starts = [...ids.keys()].filter((id) => !pred.has(id));
  if (starts.length !== 1) {
    add(
      ["edges"],
      starts.length
        ? `a pipeline has exactly one first node; these have no incoming edge: ${starts.sort().join(", ")}`
        : "the edges form a cycle (loops are coming in P0b)",
    );
    return issues;
  }
  const seen = [starts[0]];
  for (let next = succ.get(starts[0]); next !== undefined; next = succ.get(next)) {
    if (seen.includes(next)) {
      add(["edges"], "the edges form a cycle (loops are coming in P0b)");
      return issues;
    }
    seen.push(next);
  }
  for (const [id, i] of ids) {
    if (!seen.includes(id)) add(["nodes", i, "id"], `"${id}" is not reachable from the start`);
  }
  return issues;
}

/** Both client checks, deduplicated by place and message. */
export function validate(
  spec: CompositionSpec,
  schema: JsonSchema | null,
  ctx: CheckContext,
): Issue[] {
  const all = [
    ...(schema ? validateAgainstSchema(spec, schema) : []),
    ...checkComposition(spec, ctx),
  ];
  const seen = new Set<string>();
  return all.filter((issue) => {
    const key = `${JSON.stringify(issue.loc)}|${issue.msg}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** "nodes › writer › tools › 0" — a readable path, with the node's id for its index. */
export function describeLoc(loc: Issue["loc"], spec: CompositionSpec): string {
  const parts = loc.map((part, i) =>
    i === 1 && loc[0] === "nodes" && typeof part === "number"
      ? (spec.nodes[part]?.id ?? part)
      : part,
  );
  return parts.length ? parts.join(" › ") : "composition";
}
