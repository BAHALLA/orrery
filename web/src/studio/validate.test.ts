import { describe, expect, it } from "vitest";
import schema from "./fixtures/studio-schema.json";
import { P0A } from "./kinds";
import { RELEASE_NOTES, TEMPLATES, templateNeeds } from "./templates";
import { DEMO_REGISTRY } from "./fixtures/registry";
import type { CompositionSpec, JsonSchema } from "./types";
import {
  checkComposition,
  describeLoc,
  fromServer,
  groupIssues,
  targetOf,
  validate,
  validateAgainstSchema,
} from "./validate";
import demoSpec from "../../../deploy/studio-demo/release-notes.spec.json";

// The real published schema, dumped from the server (GET /studio/schema).
const SPEC_SCHEMA = (schema as { spec: JsonSchema }).spec;

const ctx = { caps: P0A, registry: DEMO_REGISTRY };

function withNode(patch: Record<string, unknown>, index = 1): CompositionSpec {
  return {
    ...RELEASE_NOTES,
    nodes: RELEASE_NOTES.nodes.map((n, i) => (i === index ? { ...n, ...patch } : n)),
  };
}

describe("templates", () => {
  it("ship the demo spec verbatim", () => {
    expect(RELEASE_NOTES).toEqual(demoSpec);
  });

  it("enable what P0a runs and mark the rest as needing P0b", () => {
    const enabled = TEMPLATES.filter((t) => templateNeeds(t, P0A) === null).map((t) => t.id);
    expect(enabled).toEqual(["pipeline", "release-notes"]);
    for (const t of TEMPLATES.filter((t) => !enabled.includes(t.id))) {
      expect(templateNeeds(t, P0A)).toBe("P0b");
    }
  });

  it("are all well-formed against the published schema, P0b ones included", () => {
    for (const t of TEMPLATES) expect(validateAgainstSchema(t.spec, SPEC_SCHEMA)).toEqual([]);
  });

  it("light up when the server runs more", () => {
    const later = {
      ...P0A,
      kinds: new Set([...P0A.kinds, "router" as const]),
      patterns: new Set([...P0A.patterns, "router" as const]),
      routing: true,
    };
    expect(
      templateNeeds(
        TEMPLATES.find((t) => t.id === "router")!,
        later,
      ),
    ).toBeNull();
  });

  it("the runnable ones pass every client check against the demo registry", () => {
    for (const t of TEMPLATES.filter((t) => templateNeeds(t, P0A) === null)) {
      expect(validate(t.spec, SPEC_SCHEMA, ctx)).toEqual([]);
    }
  });
});

describe("schema checks", () => {
  it("flag a bad id, a missing instruction and an unknown key, at their paths", () => {
    const spec = withNode({ id: "Writer", instruction: "", colour: "red" });
    const issues = validateAgainstSchema(spec, SPEC_SCHEMA);
    expect(issues.map((i) => [i.loc, i.code])).toEqual(
      expect.arrayContaining([
        [["nodes", 1, "id"], "invalid"],
        [["nodes", 1, "instruction"], "invalid"],
        [["nodes", 1, "colour"], "extra_forbidden"],
      ]),
    );
    expect(issues.find((i) => i.loc[2] === "instruction")!.msg).toBe("is required");
  });

  it("follow the node kind's own schema", () => {
    const spec = withNode({ kind: "router" });
    const issues = validateAgainstSchema(spec, SPEC_SCHEMA);
    // A router has no instruction, description aside: the other keys are unknown to it.
    expect(issues.map((i) => i.loc.join("."))).toEqual(
      expect.arrayContaining(["nodes.1.instruction", "nodes.1.tools"]),
    );
    expect(validateAgainstSchema(withNode({ kind: "robot" }), SPEC_SCHEMA)[0]).toMatchObject({
      loc: ["nodes", 1, "kind"],
    });
  });

  it("check bounds, nullable fields and edges' aliases", () => {
    const spec: CompositionSpec = {
      ...RELEASE_NOTES,
      budgets: { max_tokens: 0, timeout_s: null },
      edges: [{ from: "research", to: "writer", max_iterations: 9 }],
    };
    const locs = validateAgainstSchema(spec, SPEC_SCHEMA).map((i) => i.loc.join("."));
    expect(locs).toEqual(["budgets.max_tokens", "edges.0.max_iterations"]);
  });
});

describe("this release's rules", () => {
  it("refuse a branching pipeline at the edge that branches", () => {
    const spec = {
      ...RELEASE_NOTES,
      edges: [...RELEASE_NOTES.edges, { from: "research", to: "critic" }],
    };
    expect(checkComposition(spec, ctx)).toEqual([
      expect.objectContaining({
        loc: ["edges", 2, "from"],
        msg: expect.stringMatching(/does not branch/),
      }),
    ]);
  });

  it("refuse more than one first node and an unreachable node", () => {
    const spec = { ...RELEASE_NOTES, edges: [{ from: "research", to: "writer" }] };
    expect(checkComposition(spec, ctx)[0]).toMatchObject({
      loc: ["edges"],
      msg: expect.stringMatching(/critic, research/),
    });
  });

  it("refuse kinds, models and conditions that come later, at their field", () => {
    const spec = withNode({ model: "gpt" });
    expect(checkComposition(spec, ctx)).toEqual([
      expect.objectContaining({ loc: ["nodes", 1, "model"], code: "not_supported_yet" }),
    ]);
    const router = {
      ...RELEASE_NOTES,
      nodes: [...RELEASE_NOTES.nodes, { id: "route", kind: "router" as const }],
    };
    expect(checkComposition(router, ctx)[0]).toMatchObject({
      loc: ["nodes", 3, "kind"],
      msg: expect.stringMatching(/P0b/),
    });
  });

  it("check tool references against the registry, read-only only", () => {
    const spec = withNode({
      tools: [
        "a2a:demo-writer.deploy",
        "mcp:tirraflow.publish",
        "catalog:k8s.restart_deployment",
        "nope",
        "catalog:get_consumer_lag",
      ],
    });
    const issues = checkComposition(spec, ctx);
    expect(issues.map((i) => [i.loc.join("."), i.code])).toEqual([
      ["nodes.1.tools.0", "unknown_tool"],
      ["nodes.1.tools.1", "unknown_tool"],
      ["nodes.1.tools.2", "unknown_tool"],
      ["nodes.1.tools.3", "unknown_tool"],
    ]);
    expect(issues[1]!.msg).toMatch(/read_only/);
    // Before the registry loads, references are left to the server.
    expect(checkComposition(spec, { caps: P0A, registry: null })).toEqual([]);
  });
});

describe("server errors", () => {
  const wire = [
    {
      loc: ["spec", "nodes", 1, "tools", 0],
      msg: "remote agent 'demo-writer' is not registered for skill 'deploy'",
      code: "unknown_tool",
    },
    {
      loc: ["spec", "budgets", "max_tokens"],
      msg: "exceeds this deployment's ceiling",
      code: "over_ceiling",
    },
    { loc: ["spec", "edges", 0, "to"], msg: "no node 'x'", code: "invalid" },
    { loc: ["input"], msg: "String should have at least 1 character", code: "string_too_short" },
  ];

  it("lose the request body's spec prefix", () => {
    expect(fromServer(wire).map((i) => i.loc)).toEqual([
      ["nodes", 1, "tools", 0],
      ["budgets", "max_tokens"],
      ["edges", 0, "to"],
      ["input"],
    ]);
  });

  it("land on the node, field, edge, budget and input they name", () => {
    const issues = fromServer(wire);
    expect(issues.map((i) => targetOf(i.loc, RELEASE_NOTES))).toEqual([
      { kind: "node", nodeId: "writer", field: "tools" },
      { kind: "spec", field: "max_tokens" },
      { kind: "edge", index: 0, field: "to" },
      { kind: "input" },
    ]);
    const map = groupIssues(issues, RELEASE_NOTES);
    expect(map.byNode.get("writer")).toHaveLength(1);
    expect(map.byEdge.get(0)).toHaveLength(1);
    expect(map.spec).toHaveLength(1);
    expect(map.input).toHaveLength(1);
    expect(map.count).toBe(4);
  });

  it("read with the node's id in place of its index", () => {
    expect(describeLoc(["nodes", 1, "tools", 0], RELEASE_NOTES)).toBe("nodes › writer › tools › 0");
    expect(describeLoc([], RELEASE_NOTES)).toBe("composition");
  });

  it("an index past the spec falls back to the composition", () => {
    expect(targetOf(["nodes", 9, "id"], RELEASE_NOTES)).toEqual({ kind: "spec", field: "nodes" });
  });
});
