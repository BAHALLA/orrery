import { kindPhase, patternPhase, type Capabilities } from "./kinds";
import type { CompositionSpec, Pattern } from "./types";

/**
 * Pattern templates: a complete spec per pattern the AEP plans.
 *
 * Each is written in the full format, including kinds and edge fields this
 * release cannot run yet. {@link templateNeeds} decides from the server's
 * {@link Capabilities} whether one can be used, so the router or critic-loop
 * template lights up when the server runs them, with no change here.
 */
export interface Template {
  id: string;
  title: string;
  pattern: Pattern;
  blurb: string;
  spec: CompositionSpec;
}

const agent = (id: string, instruction: string, extra: Record<string, unknown> = {}) => ({
  id,
  kind: "llm_agent" as const,
  instruction,
  ...extra,
});

/** `deploy/studio-demo/specs/pipeline.json`, verbatim (a test holds them equal). */
export const RELEASE_NOTES: CompositionSpec = {
  spec_version: 1,
  name: "release-notes",
  description:
    "Research a topic on Tirraflow, have the remote writer draft notes, then critique them.",
  pattern: "pipeline",
  budgets: { max_tokens: 60000, max_tool_calls: 12, max_remote_calls: 2, timeout_s: 180 },
  nodes: [
    {
      id: "research",
      kind: "llm_agent",
      description: "Finds what Tirraflow says about the topic.",
      instruction:
        "Search Tirraflow for the topic in your input, read the best one or two articles, and answer with five to eight short factual notes, one per line, each citing the article URL. Start with a line '# <topic>'.",
      tools: ["mcp:tirraflow.search_articles", "mcp:tirraflow.get_article"],
    },
    {
      id: "writer",
      kind: "llm_agent",
      description: "Hands the notes to the remote writer agent.",
      instruction:
        "Send your input, unchanged, to the demo writer's draft tool, then answer with exactly the draft it returns.",
      tools: ["a2a:demo-writer.draft"],
    },
    {
      id: "critic",
      kind: "llm_agent",
      description: "Reviews the draft.",
      instruction:
        "Review the draft in your input: say what is missing, unclear or unsupported by a cited URL, then give an improved version.",
    },
  ],
  edges: [
    { from: "research", to: "writer" },
    { from: "writer", to: "critic" },
  ],
};

export const TEMPLATES: readonly Template[] = [
  {
    id: "pipeline",
    title: "Pipeline",
    pattern: "pipeline",
    blurb: "Each agent hands its answer to the next.",
    spec: {
      spec_version: 1,
      name: "pipeline",
      pattern: "pipeline",
      nodes: [
        agent("analyst", "List the facts in your input that matter for an on-call engineer."),
        agent("summariser", "Turn the facts in your input into a three-line summary."),
      ],
      edges: [{ from: "analyst", to: "summariser" }],
    },
  },
  {
    id: "release-notes",
    title: "Research → writer → critic",
    pattern: "pipeline",
    blurb: "The demo: an MCP search, a remote A2A writer, an LLM critic.",
    spec: RELEASE_NOTES,
  },
  {
    id: "router",
    title: "Router",
    pattern: "router",
    blurb: "Classify, then send to the specialist that fits.",
    spec: {
      spec_version: 1,
      name: "router",
      pattern: "router",
      nodes: [
        agent("classify", "Say whether the input is about Kafka or Kubernetes.", {
          output: { labels: ["kafka", "kubernetes"] },
        }),
        { id: "route", kind: "router" },
        agent("kafka", "Answer the Kafka question in your input."),
        agent("kubernetes", "Answer the Kubernetes question in your input."),
      ],
      edges: [
        { from: "classify", to: "route" },
        { from: "route", to: "kafka", when: "kafka" },
        { from: "route", to: "kubernetes", when: "kubernetes" },
      ],
    },
  },
  {
    id: "fan-out",
    title: "Fan-out / fan-in",
    pattern: "fan_out_fan_in",
    blurb: "Several agents work at once; one merges.",
    spec: {
      spec_version: 1,
      name: "fan-out-fan-in",
      pattern: "fan_out_fan_in",
      nodes: [
        { id: "split", kind: "parallel" },
        agent("logs", "Look for errors in the logs for the service in your input."),
        agent("metrics", "Look for anomalies in the metrics for the service in your input."),
        agent("events", "Look for recent cluster events for the service in your input."),
        { id: "gather", kind: "join" },
        agent("merge", "Merge the findings in your input into one diagnosis."),
      ],
      edges: [
        { from: "split", to: "logs" },
        { from: "split", to: "metrics" },
        { from: "split", to: "events" },
        { from: "logs", to: "gather" },
        { from: "metrics", to: "gather" },
        { from: "events", to: "gather" },
        { from: "gather", to: "merge" },
      ],
    },
  },
  {
    id: "supervisor",
    title: "Supervisor",
    pattern: "supervisor",
    blurb: "One agent calls the others as tools.",
    spec: {
      spec_version: 1,
      name: "supervisor",
      pattern: "supervisor",
      nodes: [
        {
          id: "lead",
          kind: "supervisor",
          instruction: "Use the researcher and the writer to answer the input.",
          children: ["researcher", "writer"],
        },
        agent("researcher", "Find the facts the input asks about."),
        agent("writer", "Write a clear answer from the facts in your input."),
      ],
      edges: [],
    },
  },
  {
    id: "critic-loop",
    title: "Critic loop",
    pattern: "critic_loop",
    blurb: "A critic sends the draft back until it passes.",
    spec: {
      spec_version: 1,
      name: "critic-loop",
      pattern: "critic_loop",
      nodes: [
        agent("writer", "Write a draft answer to your input, or improve the draft you were sent."),
        agent("critic", "Reject the draft unless every claim cites a source.", {
          output: { labels: ["approved", "rejected"] },
        }),
        { id: "verdict", kind: "router" },
        agent("publish", "Format the approved draft for publishing."),
      ],
      edges: [
        { from: "writer", to: "critic" },
        { from: "critic", to: "verdict" },
        { from: "verdict", to: "writer", when: "rejected", max_iterations: 3 },
        { from: "verdict", to: "publish", when: "approved" },
      ],
    },
  },
  {
    id: "debate",
    title: "Debate",
    pattern: "debate",
    blurb: "Two agents argue opposite sides; a judge decides.",
    spec: {
      spec_version: 1,
      name: "debate",
      pattern: "debate",
      nodes: [
        { id: "open", kind: "parallel" },
        agent("for_case", "Argue for the change in your input."),
        agent("against", "Argue against the change in your input."),
        { id: "close", kind: "join" },
        agent("judge", "Weigh both arguments and decide."),
      ],
      edges: [
        { from: "open", to: "for_case" },
        { from: "open", to: "against" },
        { from: "for_case", to: "close" },
        { from: "against", to: "close" },
        { from: "close", to: "judge" },
      ],
    },
  },
  {
    id: "map-reduce",
    title: "Map-reduce",
    pattern: "map_reduce",
    blurb: "Split the work, map each part, reduce the results.",
    spec: {
      spec_version: 1,
      name: "map-reduce",
      pattern: "map_reduce",
      nodes: [
        agent("split", "Split the incident timeline in your input into three periods."),
        { id: "map", kind: "parallel" },
        agent("part_1", "Summarise the first period."),
        agent("part_2", "Summarise the second period."),
        agent("part_3", "Summarise the third period."),
        { id: "collect", kind: "join" },
        agent("reduce", "Write one timeline from the three summaries."),
      ],
      edges: [
        { from: "split", to: "map" },
        { from: "map", to: "part_1" },
        { from: "map", to: "part_2" },
        { from: "map", to: "part_3" },
        { from: "part_1", to: "collect" },
        { from: "part_2", to: "collect" },
        { from: "part_3", to: "collect" },
        { from: "collect", to: "reduce" },
      ],
    },
  },
  {
    id: "handoff",
    title: "Handoff",
    pattern: "handoff",
    blurb: "A front agent hands the conversation to a specialist.",
    spec: {
      spec_version: 1,
      name: "handoff",
      pattern: "handoff",
      nodes: [
        agent("frontline", "Answer simple questions; hand database or network ones on.", {
          output: { labels: ["done", "database", "network"] },
        }),
        { id: "handoff", kind: "router" },
        agent("database", "Take over the database question in your input."),
        agent("network", "Take over the network question in your input."),
      ],
      edges: [
        { from: "frontline", to: "handoff" },
        { from: "handoff", to: "database", when: "database" },
        { from: "handoff", to: "network", when: "network" },
      ],
    },
  },
  {
    id: "plan-execute",
    title: "Plan and execute",
    pattern: "plan_and_execute",
    blurb: "A planner writes steps; an executor runs them and may replan.",
    spec: {
      spec_version: 1,
      name: "plan-and-execute",
      pattern: "plan_and_execute",
      nodes: [
        agent("planner", "Write a numbered plan for the task in your input."),
        agent("executor", "Carry out the next step of the plan; say done or replan.", {
          output: { labels: ["done", "replan"] },
        }),
        { id: "progress", kind: "router" },
        agent("report", "Report what was done."),
      ],
      edges: [
        { from: "planner", to: "executor" },
        { from: "executor", to: "progress" },
        { from: "progress", to: "planner", when: "replan", max_iterations: 3 },
        { from: "progress", to: "report", when: "done" },
      ],
    },
  },
];

const PHASES = ["P0a", "P0b", "P0c", "P1", "P2", "P3"];
/** The later of two phases; an unknown one counts as later than any known. */
const later = (a: string, b: string) => {
  const rank = (p: string) => (PHASES.includes(p) ? PHASES.indexOf(p) : PHASES.length);
  return rank(a) >= rank(b) ? a : b;
};

/**
 * Why `spec` cannot run on a server with `caps`, as the phase that brings the
 * missing part ("P0b"), or null when it can. Used for templates and for the
 * kinds of an imported spec.
 */
export function specNeeds(spec: CompositionSpec, caps: Capabilities): string | null {
  if (!caps.known) return null; // the server's 422 decides
  let need: string | null = null;
  const bump = (phase: string) => {
    need = need === null ? phase : later(need, phase);
  };
  if (spec.pattern && !caps.patterns.has(spec.pattern)) bump(patternPhase(spec.pattern, caps));
  for (const node of spec.nodes) {
    // remote_a2a is the console's shorthand, expanded on import (normalizeSpec).
    if (node.kind !== "remote_a2a" && !caps.kinds.has(node.kind)) bump(kindPhase(node.kind, caps));
    if (node.output && !caps.routing) bump("P0b");
    if (node.model && !caps.perNodeModels) bump("P0b");
  }
  for (const edge of spec.edges) {
    if (edge.when && !caps.routing) bump("P0b");
    if (edge.max_iterations && !caps.loops) bump("P0b");
    if (edge.screen && !caps.edgeScreen) bump("P0b");
  }
  return need;
}

export function templateNeeds(template: Template, caps: Capabilities): string | null {
  return specNeeds(template.spec, caps);
}
