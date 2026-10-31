import { config } from "../config";
import type {
  ActivityResponse,
  AgentsResponse,
  CatalogResponse,
  CatalogUpdate,
  ChatRequest,
  ChatResponse,
  LearningInfo,
  MeResponse,
  PendingResponse,
  ProgressEvent,
  SelfTestResponse,
  SessionResponse,
  SessionsResponse,
  StreamEvent,
  TriageResponse,
} from "./types";
import { readSse } from "./sse";

/** A typed API error carrying the HTTP status so callers can branch on 401 etc. */
export class ApiError extends Error {
  readonly status: number;
  /** Seconds to wait before retrying, from a 429's Retry-After header. */
  readonly retryAfter: number | null;

  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }

  /** True when the failure is an auth problem the user can fix by re-entering a token. */
  get isAuth(): boolean {
    return this.status === 401 || this.status === 403;
  }

  /** True when the caller was rate limited and should simply wait. */
  get isRateLimited(): boolean {
    return this.status === 429;
  }

  /** True when retrying the identical request could plausibly succeed. */
  get isRetryable(): boolean {
    return this.status === 0 || this.status === 429 || this.status >= 500;
  }
}

interface RequestOptions {
  signal?: AbortSignal;
  /** Override the inferred verb: DELETE (no body, not a GET) or PUT (a body, not a POST). */
  method?: "DELETE" | "PUT";
}

interface StreamOptions {
  signal?: AbortSignal;
  /** Called for every event, in order, as it arrives. */
  onEvent: (event: StreamEvent) => void;
}

/**
 * Minimal, dependency-free API client for the front door.
 *
 * Holds the bearer token in memory (the caller owns persistence) and attaches
 * it to every request. Kept deliberately small: one method per endpoint, typed
 * in and out, with structured errors.
 */
export class ApiClient {
  private token: string | null;

  constructor(token: string | null = null) {
    this.token = token;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  /** `body === undefined` means a GET request; anything else is POSTed as JSON,
   * except `FormData`, which the browser encodes (and sets the boundary for). */
  private async request<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = {};
    const form = body instanceof FormData;
    if (body !== undefined && !form) headers["Content-Type"] = "application/json";
    if (this.token) headers.Authorization = `Bearer ${this.token}`;

    let res: Response;
    try {
      res = await fetch(`${config.apiBaseUrl}${path}`, {
        method: options.method ?? (body !== undefined ? "POST" : "GET"),
        headers,
        body: form ? body : body !== undefined ? JSON.stringify(body) : null,
        signal: options.signal ?? null,
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      // Network-level failure (server down, DNS, CORS). 0 = "no HTTP response".
      throw new ApiError(0, "Network error — is the server reachable?");
    }

    if (!res.ok) {
      throw new ApiError(res.status, await extractError(res), retryAfterSeconds(res));
    }

    // A 204 carries no body by definition (DELETE answers with one), so parsing
    // it would fail on the empty string rather than on anything being wrong.
    if (res.status === 204) return undefined as T;

    try {
      return (await res.json()) as T;
    } catch {
      // A 200 that won't parse means the response probably never came from the
      // API at all — most often the Vite dev server answering an unproxied path
      // with the SPA shell. That looks like success and then dies inside
      // res.json() with a bare SyntaxError no caller can interpret. Parse
      // first (so a valid body with an odd Content-Type still works), then use
      // the content type to explain what arrived instead.
      const contentType = (res.headers.get("Content-Type") ?? "").split(";")[0].trim();
      throw new ApiError(
        res.status,
        contentType && !contentType.includes("json")
          ? `Expected JSON from ${path} but the server returned ${contentType}. ` +
              "The request probably didn't reach the API — check the dev proxy or the API base URL."
          : `Malformed JSON in the response from ${path}.`,
      );
    }
  }

  /** Send one chat turn. Pass the returned session_id back on the next call. */
  chat(req: ChatRequest, options?: RequestOptions): Promise<ChatResponse> {
    return this.request<ChatResponse>("/chat", chatBody(req), options);
  }

  /**
   * Send one chat turn and narrate it as it runs (POST /chat/stream, SSE).
   *
   * Resolves with the same {@link ChatResponse} {@link chat} would, once the
   * turn is over; `onEvent` sees the session id, tool progress and answer text
   * along the way. Degrades instead of failing against a server that can't
   * stream: a 404 (no such route) falls back to {@link chat}, and a JSON answer
   * (a proxy or test double that ignored the stream) is taken as the reply.
   */
  async chatStream(req: ChatRequest, { signal, onEvent }: StreamOptions): Promise<ChatResponse> {
    const body = chatBody(req);
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (!(body instanceof FormData)) headers["Content-Type"] = "application/json";
    if (this.token) headers.Authorization = `Bearer ${this.token}`;

    let res: Response;
    try {
      res = await fetch(`${config.apiBaseUrl}/chat/stream`, {
        method: "POST",
        headers,
        body: body instanceof FormData ? body : JSON.stringify(body),
        signal: signal ?? null,
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      throw new ApiError(0, "Network error — is the server reachable?");
    }

    if (res.status === 404) return this.chat(req, signal ? { signal } : {});
    if (!res.ok) {
      throw new ApiError(res.status, await extractError(res), retryAfterSeconds(res));
    }

    const contentType = res.headers.get("Content-Type") ?? "";
    if (!contentType.includes("text/event-stream") || !res.body) {
      const reply = (await res.json()) as ChatResponse;
      onEvent({ type: "session", session_id: reply.session_id });
      onEvent({ type: "message", session_id: reply.session_id, text: reply.response });
      return reply;
    }

    let sessionId = req.session_id ?? "";
    let final: ChatResponse | null = null;
    for await (const frame of readSse(res.body)) {
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(frame.data) as Record<string, unknown>;
      } catch {
        continue; // a malformed frame is skipped, not fatal to the turn
      }
      switch (frame.event) {
        case "session":
          sessionId = str(data.session_id, sessionId);
          onEvent({ type: "session", session_id: sessionId });
          break;
        case "progress":
          onEvent({ type: "progress", step: data as unknown as ProgressEvent });
          break;
        case "delta":
          onEvent({ type: "delta", text: str(data.text, ""), author: str(data.author, "") });
          break;
        case "message":
          final = { session_id: str(data.session_id, sessionId), response: str(data.text, "") };
          onEvent({ type: "message", session_id: final.session_id, text: final.response });
          break;
        case "error":
          // Status 502: the server answered, the turn behind it failed. Retryable
          // by the same rule as any 5xx, which is what the server says it is.
          throw new ApiError(502, str(data.message, "The agent failed."));
        case "done":
          // Always last: the turn is over and everything it did is recorded.
          if (final !== null) onEvent({ type: "done" });
          break;
      }
    }

    if (final === null) {
      // The stream ended without an answer: the connection dropped mid-turn.
      // The server keeps working, so the conversation will have the reply.
      throw new ApiError(
        0,
        "The connection dropped before the answer arrived. Reopen the conversation in a moment to see it.",
      );
    }
    return final;
  }

  /** The caller's conversations, newest first. No transcripts — see {@link session}. */
  sessions(options?: RequestOptions): Promise<SessionsResponse> {
    return this.request<SessionsResponse>("/sessions", undefined, options);
  }

  /** One conversation with its full transcript, rebuilt server-side from the events. */
  session(sessionId: string, options?: RequestOptions): Promise<SessionResponse> {
    return this.request<SessionResponse>(
      `/session/${encodeURIComponent(sessionId)}`,
      undefined,
      options,
    );
  }

  /** Delete one of the caller's conversations. Answers 204, so nothing is returned. */
  deleteSession(sessionId: string, options?: RequestOptions): Promise<void> {
    return this.request<void>(`/session/${encodeURIComponent(sessionId)}`, undefined, {
      ...options,
      method: "DELETE",
    });
  }

  /** Tool-call timeline for one of the caller's sessions. */
  activity(sessionId: string, options?: RequestOptions): Promise<ActivityResponse> {
    return this.request<ActivityResponse>(
      `/session/${encodeURIComponent(sessionId)}/activity`,
      undefined,
      options,
    );
  }

  /** The caller's own guarded action awaiting approve/deny, if any. */
  pendingConfirmation(options?: RequestOptions): Promise<PendingResponse> {
    return this.request<PendingResponse>("/confirmations/pending", undefined, options);
  }

  /** The session's latest triage verdict (severity + report), if any. */
  triage(sessionId: string, options?: RequestOptions): Promise<TriageResponse> {
    return this.request<TriageResponse>(
      `/session/${encodeURIComponent(sessionId)}/triage`,
      undefined,
      options,
    );
  }

  /** The server's own view of the caller: resolved role, autonomy level, model. */
  me(options?: RequestOptions): Promise<MeResponse> {
    return this.request<MeResponse>("/me", undefined, options);
  }

  /** Raise the caller's hint level by one step (learning sandbox only). */
  takeHint(options?: RequestOptions): Promise<LearningInfo> {
    return this.request<LearningInfo>("/learning/hint", {}, options);
  }

  /** Every specialist the coordinator can reach, with tool guard levels (admin only). */
  agents(options?: RequestOptions): Promise<AgentsResponse> {
    return this.request<AgentsResponse>("/admin/agents", undefined, options);
  }

  /** The admin-editable agent catalog and what it may contain (admin only). */
  catalog(options?: RequestOptions): Promise<CatalogResponse> {
    return this.request<CatalogResponse>("/admin/catalog", undefined, options);
  }

  /** Replace the catalog. 409 if it changed since `version`, 422 if it can't be built. */
  updateCatalog(update: CatalogUpdate, options?: RequestOptions): Promise<CatalogResponse> {
    return this.request<CatalogResponse>("/admin/catalog", update, { ...options, method: "PUT" });
  }

  /** Run the first-run environment check (model + each wired integration). */
  selfTest(options?: RequestOptions): Promise<SelfTestResponse> {
    return this.request<SelfTestResponse>("/onboarding/selftest", {}, options);
  }
}

/** A string field from a parsed frame, or `fallback` when it is missing or not a string. */
function str(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

/** Parse `Retry-After` (delta-seconds or an HTTP date) into whole seconds. */
export function retryAfterSeconds(res: Response): number | null {
  const header = res.headers.get("Retry-After");
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return Math.max(0, Math.round(seconds));
  const when = Date.parse(header);
  return Number.isNaN(when) ? null : Math.max(0, Math.round((when - Date.now()) / 1000));
}

/** JSON for a plain turn; multipart when it carries files (AEP-030). */
function chatBody({ files, ...json }: ChatRequest): FormData | Omit<ChatRequest, "files"> {
  if (!files?.length) return json;
  const form = new FormData();
  form.append("message", json.message);
  if (json.session_id) form.append("session_id", json.session_id);
  for (const file of files) form.append("files", file, file.name);
  return form;
}

/** Pull a human-readable message out of a non-2xx response, tolerating any shape. */
export async function extractError(res: Response): Promise<string> {
  try {
    const data: unknown = await res.json();
    if (data && typeof data === "object" && "detail" in data) {
      const { detail } = data;
      if (typeof detail === "string") return detail;
      // FastAPI request validation: a list of {loc, msg}. Name the fields, so
      // "agents → 0 → name: …" says what to fix instead of "Unprocessable Entity".
      if (Array.isArray(detail)) {
        const messages = detail.flatMap((d: unknown) => {
          if (!d || typeof d !== "object" || !("msg" in d) || typeof d.msg !== "string") return [];
          const loc = "loc" in d && Array.isArray(d.loc) ? d.loc.filter((x) => x !== "body") : [];
          return [loc.length ? `${loc.join(" → ")}: ${d.msg}` : d.msg];
        });
        if (messages.length) return messages.join("; ");
      }
    }
  } catch {
    // fall through to status text
  }
  return res.statusText || `Request failed (${res.status})`;
}
