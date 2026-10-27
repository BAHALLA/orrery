import {
  PATTERNS,
  type NodeKind,
  type Pattern,
  type ServerCapabilities,
  type SpecNode,
} from "./types";

/**
 * What this server can run, as the console reads it.
 *
 * The format describes every phase's node kinds and patterns, but a release
 * runs only some of them. The palette, the templates and the client checks all
 * ask {@link Capabilities} rather than hard-coding a phase. The server publishes
 * them in `GET /studio/schema` ({@link capabilitiesFrom}); until it answers, the
 * console draws what {@link P0A} allows (the least any Studio runs) but does not
 * flag the rest as unsupported: `known` is false.
 */
export interface Capabilities {
  /** Whether the server said; when false, nothing is flagged as unsupported. */
  known: boolean;
  kinds: ReadonlySet<NodeKind>;
  patterns: ReadonlySet<Pattern>;
  /** Node `output` labels and edge `when`. */
  routing: boolean;
  /** Edge `max_iterations`. */
  loops: boolean;
  /** Edge `screen`. */
  edgeScreen: boolean;
  perNodeModels: boolean;
  /** The models a node may name (the server's allowlist). */
  models: readonly string[];
  /** The server's reason a kind or pattern is not run yet ("P0c", "P1; …"). */
  pendingKinds: ReadonlyMap<string, string>;
  pendingPatterns: ReadonlyMap<string, string>;
}

export const P0A: Capabilities = {
  known: false,
  kinds: new Set<NodeKind>(["llm_agent"]),
  patterns: new Set<Pattern>(["pipeline"]),
  routing: false,
  loops: false,
  edgeScreen: false,
  perNodeModels: false,
  models: [],
  pendingKinds: new Map(),
  pendingPatterns: new Map(),
};

/**
 * The server's capabilities, keeping only the kinds and patterns this console
 * knows: a newer server may run a kind this build cannot draw.
 */
export function capabilitiesFrom(server: ServerCapabilities | undefined | null): Capabilities {
  if (!server) return P0A;
  const known = (values: string[] | undefined, allowed: readonly string[]) =>
    (values ?? []).filter((v) => allowed.includes(v));
  const routing = server.routing === true;
  return {
    known: true,
    kinds: new Set(known(server.node_kinds, PALETTE_KINDS) as NodeKind[]),
    patterns: new Set(known(server.patterns, PATTERNS) as Pattern[]),
    routing,
    // Older servers sent `routing` alone for all three.
    loops: server.loops ?? routing,
    edgeScreen: server.edge_screen ?? routing,
    perNodeModels: server.per_node_models === true,
    models: server.per_node_models === true ? [...(server.models ?? [])] : [],
    pendingKinds: new Map(Object.entries(server.unsupported_node_kinds ?? {})),
    pendingPatterns: new Map(Object.entries(server.unsupported_patterns ?? {})),
  };
}

/** "P1" from the server's "P1; a router between agents does the same". */
const phaseIn = (reason: string | undefined) => reason?.split(";")[0].trim() || undefined;

/** The phase that brings *kind* on this server ("P0c"). */
export function kindPhase(kind: NodeKind, caps: Capabilities): string {
  return phaseIn(caps.pendingKinds.get(kind)) ?? KINDS[kind].phase;
}

/** The phase that brings *pattern* on this server ("P1" for handoff). */
export function patternPhase(pattern: Pattern, caps: Capabilities): string {
  return phaseIn(caps.pendingPatterns.get(pattern)) ?? "P0b";
}

export interface KindInfo {
  label: string;
  /** One line for the palette. */
  blurb: string;
  /** The phase that brings it, while the server cannot run it. */
  phase: string;
}

export const KINDS: Record<NodeKind, KindInfo> = {
  llm_agent: {
    label: "LLM agent",
    blurb: "An Orrery agent: an instruction and the tools it may call.",
    phase: "P0a",
  },
  remote_a2a: {
    label: "Remote agent",
    blurb: "An agent that forwards its input to one remote A2A skill.",
    phase: "P0a",
  },
  router: {
    label: "Router",
    blurb: "Picks the next node from the previous node's output label.",
    phase: "P0b",
  },
  parallel: { label: "Parallel", blurb: "Fans out to several branches at once.", phase: "P0b" },
  join: { label: "Join", blurb: "Waits for every incoming branch.", phase: "P0b" },
  supervisor: {
    label: "Supervisor",
    blurb: "An agent that calls other agents as its tools.",
    phase: "P0b",
  },
  human_approval: {
    label: "Human approval",
    blurb: "Pauses the run until an admin approves.",
    phase: "P0c",
  },
};

/** Palette order. */
export const PALETTE_KINDS: readonly NodeKind[] = [
  "llm_agent",
  "remote_a2a",
  "router",
  "parallel",
  "join",
  "supervisor",
  "human_approval",
];

/**
 * Whether the palette can add a kind. `remote_a2a` is the console's shorthand
 * for an `llm_agent` holding one `a2a:` tool (AEP-031 §1): the server refuses
 * the bare kind, so the console expands it before it reaches the spec.
 */
export function canAdd(kind: NodeKind, caps: Capabilities): boolean {
  return kind === "remote_a2a" ? caps.kinds.has("llm_agent") : caps.kinds.has(kind);
}

/** "coming in P0c" for a kind this server cannot run yet, else null. */
export function comingIn(kind: NodeKind, caps: Capabilities): string | null {
  return canAdd(kind, caps) ? null : `coming in ${kindPhase(kind, caps)}`;
}

/** The id a new node of `kind` starts from: `agent`, `remote`, `router`… */
export function idStem(kind: NodeKind): string {
  return kind === "remote_a2a" ? "remote" : kind === "llm_agent" ? "agent" : kind;
}

/** A fresh node of `kind` with id `id`. `remote_a2a` expands to an `llm_agent`. */
export function newNode(kind: NodeKind, id: string, remote?: string): SpecNode {
  switch (kind) {
    case "llm_agent":
      return { id, kind, instruction: "", tools: [] };
    case "remote_a2a":
      return {
        id,
        kind: "llm_agent",
        description: "Forwards its input to a remote agent.",
        instruction:
          "Send your input, unchanged, to the remote agent's tool, then answer with exactly what it returns.",
        tools: remote ? [`a2a:${remote}`] : [],
      };
    case "supervisor":
      return { id, kind, instruction: "", children: [], tools: [] };
    case "human_approval":
      return { id, kind, prompt: "Approve this step?" };
    default:
      return { id, kind };
  }
}

/** A tool reference, parsed: `mcp:<entry>.<tool>`, `a2a:<entry>.<skill>`, `catalog:<ref>`. */
export interface ToolRef {
  kind: "mcp" | "a2a" | "catalog";
  entry: string;
  name: string;
}

/** Same rules as the server's `parse_tool_ref`; null when malformed. */
export function parseToolRef(ref: string): ToolRef | null {
  const colon = ref.indexOf(":");
  if (colon <= 0 || colon === ref.length - 1) return null;
  const kind = ref.slice(0, colon);
  const rest = ref.slice(colon + 1);
  if (kind === "catalog") return { kind, entry: "", name: rest };
  if (kind === "mcp" || kind === "a2a") {
    const dot = rest.indexOf(".");
    if (dot > 0 && dot < rest.length - 1) {
      return { kind, entry: rest.slice(0, dot), name: rest.slice(dot + 1) };
    }
  }
  return null;
}

/** Short label for a chip: `tirraflow.search_articles`, `get_consumer_lag`. */
export function toolLabel(ref: string): string {
  const parsed = parseToolRef(ref);
  if (!parsed) return ref;
  return parsed.kind === "catalog" ? parsed.name : `${parsed.entry}.${parsed.name}`;
}
