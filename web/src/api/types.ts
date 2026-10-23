/**
 * Wire types for the Orrery HTTP front door.
 *
 * These mirror `core/orrery_core/serving/server.py` (ChatRequest / ChatResponse).
 * Until AEP-019's typed-boundary work generates these from the FastAPI OpenAPI
 * schema (`openapi-typescript`), they are hand-maintained — keep them in sync
 * with the server models.
 */

/** POST /chat request body. */
export interface ChatRequest {
  message: string;
  /** Omit / null on the first turn; the server mints and returns a session id. */
  session_id?: string | null;
  /** Attachments (AEP-030). With any, the turn is sent as multipart/form-data. */
  files?: File[];
}

/** POST /chat success response. */
export interface ChatResponse {
  session_id: string;
  response: string;
}

/**
 * One edge of one tool call, streamed while a turn runs (ProgressStep server-side).
 * Names and outcomes only — the server never streams tool arguments or results.
 */
export interface ProgressEvent {
  /** Pairs a `start` with its `end`. */
  call_id: string;
  phase: "start" | "end";
  /** The agent that made the call. */
  agent: string;
  /** The tool. For a delegation, the specialist being asked. */
  tool: string;
  /** Hands work to another agent; later steps whose `agent` is `tool` ran inside it. */
  delegation: boolean;
  /** `@confirm` / `@destructive`. */
  guarded: boolean;
  /** On `end`: ok, error (the tool reported failure) or refused (a gate answered). */
  outcome: "ok" | "error" | "refused" | null;
  status: string;
  elapsed_ms: number | null;
}

/** The events POST /chat/stream emits (see core/orrery_core/serving/sse.py). */
export type StreamEvent =
  | { type: "session"; session_id: string }
  | { type: "progress"; step: ProgressEvent }
  | { type: "delta"; text: string; author: string }
  | { type: "message"; session_id: string; text: string }
  | { type: "error"; message: string; retryable: boolean }
  | { type: "done" };

/** One of the caller's conversations, as GET /sessions lists it (no transcript). */
export interface SessionSummary {
  session_id: string;
  /** Empty when nothing labelled the session — the client supplies the placeholder. */
  title: string;
  /** Unix *seconds* of the last write (the server's clock, not the browser's). */
  last_update_time: number;
}

/** GET /sessions response. */
export interface SessionsResponse {
  sessions: SessionSummary[];
}

/** One stored message from a session's transcript. */
export interface SessionMessage {
  role: "user" | "assistant";
  text: string;
  /** Unix *seconds*. */
  at: number;
}

/** GET /session/{id} response — one conversation, transcript included. */
export interface SessionResponse {
  session_id: string;
  title: string;
  messages: SessionMessage[];
  last_update_time: number;
}

/** The three roles the platform's RBAC resolves to. */
export type Role = "viewer" | "operator" | "admin";

/** One recorded tool call (shape written by ActivityPlugin server-side). */
export interface ActivityEntry {
  operation: string;
  details: string;
  timestamp: string;
}

/** GET /session/{id}/activity response. */
export interface ActivityResponse {
  session_id: string;
  entries: ActivityEntry[];
}

/** The caller's guarded action awaiting an approve/deny decision. */
export interface PendingConfirmation {
  tool_name: string;
  level: string;
  args: Record<string, unknown>;
  created_at: number;
}

/** GET /confirmations/pending response. */
export interface PendingResponse {
  pending: PendingConfirmation | null;
}

/** Triage severity verdict recorded by record_triage_verdict. */
export type TriageSeverity = "healthy" | "degraded" | "critical";

/** GET /session/{id}/triage response. */
export interface TriageResponse {
  session_id: string;
  severity: TriageSeverity | null;
  report: string | null;
}

/** GET /me — the server's own view of the caller and the deployment. */
export interface MeResponse {
  subject: string;
  /** Resolved server-side. The token badge is advisory; this is authoritative. */
  role: Role;
  /** "L2" | "L3" | "L4", or null when the autonomy gate isn't configured. */
  autonomy_level: string | null;
  model_provider: string;
  model_name: string;
  self_test_available: boolean;
  /** What a turn may attach (AEP-030); null when attachments are off. */
  attachments?: AttachmentsInfo | null;
}

/** The server's attachment limits. The server re-checks everything by content. */
export interface AttachmentsInfo {
  max_files: number;
  max_file_bytes: number;
  max_total_bytes: number;
  images: boolean;
  image_types: string[];
}

/** How a tool is gated. `dynamic`: an MCP toolset, classified per tool at run time. */
export type ToolGuard = "read" | "confirm" | "destructive" | "dynamic";

/** One agent the coordinator can delegate to (GET /admin/agents). */
export interface InventoryAgent {
  name: string;
  description: string;
  /** builtin | plugin | declarative | mcp */
  source: string;
  tools: { name: string; guard: ToolGuard; reason: string }[];
}

/** GET /admin/agents response (admin only). */
export interface AgentsResponse {
  agents: InventoryAgent[];
}

/** A declarative agent: a brief over existing tools (no new code). */
export interface DeclarativeAgent {
  name: string;
  description: string;
  instruction: string;
  tools: string[];
}

/** The admin-editable agent set (GET/PUT /admin/catalog). */
export interface AgentCatalog {
  version: number;
  disabled_specialists: string[];
  agents: DeclarativeAgent[];
  updated_by: string;
  updated_at: number;
}

export interface CatalogResponse {
  /** False unless the deployment sets ORRERY_ADMIN_EDITING. */
  editing_enabled: boolean;
  catalog: AgentCatalog;
  applied_version: number;
  specialists: {
    key: string;
    agent_name: string;
    description: string;
    source: string;
    /** False when the deployment's env excludes it: shown, not switchable. */
    available: boolean;
  }[];
  tools: {
    ref: string;
    specialist: string;
    guard: Exclude<ToolGuard, "dynamic">;
    reason: string;
  }[];
  /** Agents from ORRERY_AGENTS_CONFIG: fixed here, names reserved. */
  file_agents: string[];
}

export interface CatalogUpdate {
  version: number;
  disabled_specialists: string[];
  agents: DeclarativeAgent[];
}

/** One integration or connectivity check from the self-test. */
export interface CheckResult {
  name: string;
  label: string;
  ok: boolean;
  detail: string;
  /** What to configure when `ok` is false; empty when it passed. */
  hint: string;
  duration_ms: number;
}

/** POST /onboarding/selftest response. */
export interface SelfTestResponse {
  ok: boolean;
  checks: CheckResult[];
}
