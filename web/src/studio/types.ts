/**
 * Wire types for the Studio API (AEP-031, `/studio/*`).
 *
 * These mirror `core/orrery_core/studio/{spec,events,api}.py`. The server also
 * publishes the spec and event schemas at `GET /studio/schema`, which the
 * console validates against at run time ({@link validateAgainstSchema}); these
 * types are the compile-time half of the same contract. Keep them in sync.
 */

/** Every node kind the format describes. P0a runs `llm_agent` only. */
export type NodeKind =
  "llm_agent" | "remote_a2a" | "router" | "parallel" | "join" | "supervisor" | "human_approval";

export type Pattern =
  | "pipeline"
  | "fan_out_fan_in"
  | "router"
  | "supervisor"
  | "critic_loop"
  | "debate"
  | "map_reduce"
  | "handoff"
  | "plan_and_execute";

/**
 * One node, any kind. Fields belong to some kinds only (the server refuses an
 * unknown key for a kind); the console keeps whatever a spec carries so an
 * import round-trips unchanged.
 */
export interface SpecNode {
  id: string;
  kind: NodeKind;
  description?: string;
  /** llm_agent, supervisor, remote_a2a. */
  instruction?: string;
  /** llm_agent, supervisor: `mcp:<entry>.<tool>`, `a2a:<entry>.<skill>`, `catalog:<ref>`. */
  tools?: string[];
  /** P0b: a model from ORRERY_STUDIO_MODELS. */
  model?: string | null;
  /** P0b: labels a router reads. */
  output?: { labels: string[] } | null;
  /** supervisor (P0b): the node ids it calls as tools. */
  children?: string[];
  /** human_approval (P0c). */
  prompt?: string;
  /** remote_a2a shorthand: `<entry>.<skill>`. */
  remote?: string;
}

export interface SpecEdge {
  from: string;
  to: string;
  /** P0b: the source's output label this edge is taken on. */
  when?: string | null;
  /** P0b: a back edge's iteration cap. */
  max_iterations?: number | null;
  /** P0b: screen the text crossing this edge. */
  screen?: boolean;
}

export const BUDGET_NAMES = [
  "max_tokens",
  "max_tool_calls",
  "max_delegations",
  "max_remote_calls",
  "timeout_s",
] as const;
export type BudgetName = (typeof BUDGET_NAMES)[number];

/** Whole-run limits. Unset: the deployment's ceiling. */
export type Budgets = Partial<Record<BudgetName, number | null>>;

export interface CompositionSpec {
  spec_version: number;
  name: string;
  description?: string;
  pattern?: Pattern;
  budgets?: Budgets;
  nodes: SpecNode[];
  edges: SpecEdge[];
}

/** One field-level problem, as `POST /studio/runs` answers a 422 with it. */
export interface SpecErrorWire {
  loc: (string | number)[];
  msg: string;
  code: string;
}

/** `GET /studio/schema`. */
export interface StudioSchema {
  spec_version: number;
  spec: JsonSchema;
  events: JsonSchema;
}

/** The subset of JSON Schema (draft 2020-12, as Pydantic emits it) the console reads. */
export interface JsonSchema {
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  type?: string;
  const?: unknown;
  enum?: unknown[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean | JsonSchema;
  items?: JsonSchema;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  discriminator?: { propertyName: string; mapping?: Record<string, string> };
  pattern?: string;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  minimum?: number;
  maximum?: number;
  title?: string;
  description?: string;
  default?: unknown;
}

// ── Registry (GET /studio/registry) ──────────────────────────────────

export interface RegistryMcpTool {
  name: string;
  /** `read_only`, or `destructive` (the MCP default): only read-only ones run in P0a. */
  classification: string;
  pinned: boolean;
}

export interface RegistryMcpServer {
  id: string;
  url: string;
  transport: string;
  private_network: boolean;
  tools: RegistryMcpTool[];
}

export interface CardSkill {
  id: string;
  name: string;
  description: string;
}

/** What the server knows of a remote's agent card: the pin, never the card bytes. */
export interface CardStatus {
  /** SHA-256 of the card's trust fields (RFC 8785), pinned or from config. */
  digest: string | null;
  /** `config` (card_sha256 set), `first_use`, or null when not pinned yet. */
  pinned_by: string | null;
  resolved: boolean;
  fetched_at: number | null;
  /** Why the card was refused, last time it was fetched. */
  error: string | null;
  skills: CardSkill[];
}

export interface RegistryA2aEntry {
  id: string;
  card_url: string;
  /** The card's skills this entry may use. */
  skills: string[];
  effect: string;
  private_network: boolean;
  authenticated: boolean;
  description: string;
  card: CardStatus;
}

export interface RegistryCatalogTool {
  ref: string;
  read_only: boolean;
}

export interface StudioRegistry {
  mcp: RegistryMcpServer[];
  a2a: RegistryA2aEntry[];
  catalog: RegistryCatalogTool[];
}

// ── Runs ─────────────────────────────────────────────────────────────

export type RunOutcome = "succeeded" | "failed" | "cancelled" | "budget_exhausted";
export type NodeOutcome = "ok" | "failed" | "cancelled" | "budget_exhausted";

/** `GET /studio/runs/{id}`. */
export interface RunSummary {
  run_id: string;
  status: "running" | RunOutcome;
  spec_name: string;
  spec_digest: string;
  created_at: number;
  ended_at: number | null;
  error: string | null;
  totals: Totals | null;
  event_count: number;
  dropped_events: number;
}

export interface TokenCount {
  input: number;
  output: number;
  total: number;
}

export interface Totals {
  tokens: TokenCount;
  /** Model label (`a → b` for a fallback chain) -> tokens. */
  tokens_by_model: Record<string, TokenCount>;
  tool_calls: number;
  delegations: number;
  remote_calls: number;
  wall_ms: number;
}

interface EventBase {
  /** 1, 2, … within the run; the SSE `id`. */
  seq: number;
  run_id: string;
  /** Unix seconds. */
  ts: number;
}

export interface RunStartEvent extends EventBase {
  type: "run.start";
  spec_name: string;
  spec_digest: string;
  pattern: string;
  /** Node ids in run order. */
  nodes: string[];
  /** Resolved budgets (the spec's, or the deployment's ceilings). */
  budgets: Record<string, number>;
  content?: string;
}

export interface RunEndEvent extends EventBase {
  type: "run.end";
  outcome: RunOutcome;
  error?: string;
  totals: Totals;
  dropped_events?: number;
  content?: string;
}

export interface NodeEnterEvent extends EventBase {
  type: "node.enter";
  node_id: string;
  kind: string;
  model: string;
}

export interface NodeExitEvent extends EventBase {
  type: "node.exit";
  node_id: string;
  kind: string;
  model: string;
  tokens: TokenCount;
  elapsed_ms: number;
  outcome: NodeOutcome;
  content?: string;
}

export interface PartSummary {
  kind: string;
  size: number;
}

export interface A2aMessageEvent extends EventBase {
  type: "a2a.message";
  node_id: string;
  remote: string;
  skill: string;
  direction: "outbound" | "inbound";
  method: string;
  task_id?: string;
  context_id?: string;
  task_state?: string;
  /** Task states seen during the exchange, in order (inbound only). */
  states?: string[];
  parts?: PartSummary[];
  elapsed_ms?: number;
  /** The tool's result status (inbound), or `refused` when nothing was sent. */
  status?: string;
  card_digest?: string;
  content?: string;
}

export interface McpCallEvent extends EventBase {
  type: "mcp.call";
  node_id: string;
  entry: string;
  tool: string;
  /** `read_only` or the guard level the tool carries. */
  classification: string;
  /** `success` / `failure` / `refused` (a gate or budget answered). */
  outcome: string;
  status?: string;
  elapsed_ms?: number;
}

export interface BudgetHitEvent extends EventBase {
  type: "budget.hit";
  budget: BudgetName;
  limit: number;
  used: number;
}

export type StudioEvent =
  | RunStartEvent
  | RunEndEvent
  | NodeEnterEvent
  | NodeExitEvent
  | A2aMessageEvent
  | McpCallEvent
  | BudgetHitEvent;
