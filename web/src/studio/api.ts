import { ApiError, extractError, retryAfterSeconds } from "../api/client";
import { readSse } from "../api/sse";
import { config } from "../config";
import type {
  CompositionRecord,
  CompositionSpec,
  CompositionSummary,
  PendingApproval,
  RegistryDocument,
  RegistryVersionSummary,
  RunSummary,
  SpecErrorWire,
  StudioEvent,
  StudioRegistry,
  StudioSchema,
} from "./types";

/**
 * The Studio's API client (`/studio/*`, admin and human only).
 *
 * Kept apart from {@link ApiClient} so it ships in the Studio's lazy chunk,
 * not in the chat console's first load. Same conventions: bearer token in
 * memory, {@link ApiError} for every failure, one method per route.
 */

/** A 422 from `POST /studio/runs`: every field-level problem the server found. */
export class SpecRefused extends Error {
  readonly errors: SpecErrorWire[];

  constructor(errors: SpecErrorWire[]) {
    super(
      `The server refused the composition (${errors.length} problem${errors.length === 1 ? "" : "s"}).`,
    );
    this.name = "SpecRefused";
    this.errors = errors;
  }
}

/** The `{"detail": {"errors": [...]}}` body, or null for any other shape. */
export function specErrors(body: unknown): SpecErrorWire[] | null {
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const { detail } = body;
  if (!detail || typeof detail !== "object" || !("errors" in detail)) return null;
  const { errors } = detail;
  if (!Array.isArray(errors)) return null;
  return errors.flatMap((e: unknown) => {
    if (!e || typeof e !== "object") return [];
    const { loc, msg, code } = e as Record<string, unknown>;
    if (!Array.isArray(loc) || typeof msg !== "string") return [];
    const path = loc.filter(
      (p): p is string | number => typeof p === "string" || typeof p === "number",
    );
    return [{ loc: path, msg, code: typeof code === "string" ? code : "invalid" }];
  });
}

/**
 * A 409 from a versioned write: someone saved after the version this edit
 * started from. `currentVersion` is theirs, when the server says.
 */
export class VersionConflict extends ApiError {
  readonly currentVersion: number | null;

  constructor(message: string, currentVersion: number | null) {
    super(409, message);
    this.name = "VersionConflict";
    this.currentVersion = currentVersion;
  }
}

/** `{"detail": {"message", "current_version"}}`, the shape of a versioned write's 409. */
function conflictOf(body: unknown): VersionConflict | null {
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const { detail } = body;
  if (!detail || typeof detail !== "object") return null;
  const { message, current_version } = detail as Record<string, unknown>;
  return new VersionConflict(
    typeof message === "string" ? message : "It changed since you read it.",
    typeof current_version === "number" ? current_version : null,
  );
}

interface EventsOptions {
  /** The last `seq` already seen: sent as `Last-Event-ID`, so the server resumes after it. */
  after: number;
  signal?: AbortSignal;
  onEvent: (event: StudioEvent) => void;
}

export class StudioClient {
  private token: string | null;

  constructor(token: string | null) {
    this.token = token;
  }

  /**
   * Swap the bearer token in place (a silent renewal). The client lives as
   * long as the view, so a stream being followed picks up the new token on
   * its next reconnect, and nothing is fetched again.
   */
  setToken(token: string | null): void {
    this.token = token;
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    return this.token ? { ...extra, Authorization: `Bearer ${this.token}` } : extra;
  }

  private async send(path: string, init: RequestInit): Promise<Response> {
    try {
      return await fetch(`${config.apiBaseUrl}${path}`, init);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      throw new ApiError(0, "Network error — is the server reachable?");
    }
  }

  private async json<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>(path, { method: body === undefined ? "GET" : "POST", body, signal });
  }

  /**
   * One JSON call. A 422 carrying field errors throws {@link SpecRefused}, a
   * 409 carrying a version {@link VersionConflict}, anything else not ok an
   * {@link ApiError}; a 204 resolves to undefined.
   */
  private async request<T>(
    path: string,
    { method, body, signal }: { method: string; body?: unknown; signal?: AbortSignal | undefined },
  ): Promise<T> {
    const res = await this.send(path, {
      method,
      headers: this.headers(body === undefined ? {} : { "Content-Type": "application/json" }),
      body: body === undefined ? null : JSON.stringify(body),
      signal: signal ?? null,
    });
    if (res.status === 422 || res.status === 409) {
      const parsed: unknown = await res
        .clone()
        .json()
        .catch(() => null);
      const errors = res.status === 422 ? specErrors(parsed) : null;
      if (errors) throw new SpecRefused(errors);
      const conflict = res.status === 409 ? conflictOf(parsed) : null;
      if (conflict) throw conflict;
    }
    if (!res.ok) throw new ApiError(res.status, await extractError(res), retryAfterSeconds(res));
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }

  /** The spec and event JSON Schemas the console validates against. */
  schema(signal?: AbortSignal): Promise<StudioSchema> {
    return this.json<StudioSchema>("/studio/schema", undefined, signal);
  }

  /** MCP servers and their tools, remote agents and their cards, catalogue tools. */
  registry(resolve = false, signal?: AbortSignal): Promise<StudioRegistry> {
    return this.json<StudioRegistry>(
      `/studio/registry${resolve ? "?resolve=true" : ""}`,
      undefined,
      signal,
    );
  }

  /** The registry as written, its version and whether it may be edited here. */
  registryDocument(signal?: AbortSignal): Promise<RegistryDocument> {
    return this.json<RegistryDocument>("/studio/registry/document", undefined, signal);
  }

  /**
   * Save an edited registry. Throws {@link SpecRefused} on a 422 (paths under
   * `document`) and {@link ApiError} 409 when it moved past *version*.
   */
  saveRegistry(body: {
    document: Record<string, unknown>;
    version: number;
    note: string;
  }): Promise<RegistryVersionSummary> {
    return this.request("/studio/registry/document", { method: "PUT", body });
  }

  /** Saved registry versions, newest first. */
  async registryHistory(signal?: AbortSignal): Promise<RegistryVersionSummary[]> {
    const body = await this.json<{ versions: RegistryVersionSummary[] }>(
      "/studio/registry/history",
      undefined,
      signal,
    );
    return body.versions;
  }

  /** One saved version, with its document. */
  registryVersion(
    version: number,
    signal?: AbortSignal,
  ): Promise<RegistryVersionSummary & { document: Record<string, unknown> }> {
    return this.json(`/studio/registry/history/${version}`, undefined, signal);
  }

  /** Start a run. Throws {@link SpecRefused} with the server's field errors on a 422. */
  startRun(
    source: CompositionSpec | { compositionId: string; version: number },
    input: string,
  ): Promise<{ run_id: string }> {
    const body =
      "compositionId" in source
        ? { composition_id: source.compositionId, version: source.version, input }
        : { spec: source, input };
    return this.request("/studio/runs", { method: "POST", body });
  }

  /** The caller's runs, newest first: this replica's and the stored ones. */
  async runs(
    { compositionId, limit = 50 }: { compositionId?: string | undefined; limit?: number } = {},
    signal?: AbortSignal,
  ): Promise<RunSummary[]> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (compositionId) query.set("composition_id", compositionId);
    const body = await this.json<{ runs: RunSummary[] }>(
      `/studio/runs?${query}`,
      undefined,
      signal,
    );
    return body.runs;
  }

  // ── compositions (shared by every Studio admin) ──

  async compositions(signal?: AbortSignal): Promise<CompositionSummary[]> {
    const body = await this.json<{ compositions: CompositionSummary[] }>(
      "/studio/compositions",
      undefined,
      signal,
    );
    return body.compositions;
  }

  /** One version (the latest when unset), with the list of every version. */
  composition(id: string, version?: number, signal?: AbortSignal): Promise<CompositionRecord> {
    const query = version === undefined ? "" : `?version=${version}`;
    return this.json(`/studio/compositions/${encodeURIComponent(id)}${query}`, undefined, signal);
  }

  /** Save a new composition. Throws {@link SpecRefused} on a 422. */
  createComposition(spec: CompositionSpec): Promise<CompositionRecord> {
    return this.request("/studio/compositions", { method: "POST", body: { spec } });
  }

  /**
   * Save a new version of *id*, edited from *version*. Throws
   * {@link SpecRefused} on a 422 and {@link VersionConflict} when someone
   * saved since.
   */
  updateComposition(
    id: string,
    spec: CompositionSpec,
    version: number,
  ): Promise<CompositionRecord> {
    return this.request(`/studio/compositions/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: { spec, version },
    });
  }

  /** Delete *id*, if *version* is still its latest (else {@link VersionConflict}). */
  deleteComposition(id: string, version: number): Promise<void> {
    return this.request(`/studio/compositions/${encodeURIComponent(id)}?version=${version}`, {
      method: "DELETE",
    });
  }

  run(runId: string, signal?: AbortSignal): Promise<RunSummary> {
    return this.json<RunSummary>(`/studio/runs/${encodeURIComponent(runId)}`, undefined, signal);
  }

  /** The run's pending approvals, each with the text under review (owner only). */
  approvals(runId: string, signal?: AbortSignal): Promise<{ approvals: PendingApproval[] }> {
    return this.json(`/studio/runs/${encodeURIComponent(runId)}/approvals`, undefined, signal);
  }

  /**
   * Decide one. Approving names the digest of the text that was read: the
   * server refuses (409) a digest that is not the one under review. A
   * remote's question (`kind: "input"`) is answered by approving it with
   * `answer`; rejecting it declines, and the remote is sent nothing.
   */
  decide(
    runId: string,
    approvalId: string,
    body:
      | { decision: "approve"; digest: string; comment?: string; answer?: string }
      | { decision: "reject"; comment?: string },
  ): Promise<{ approval_id: string; decision: string }> {
    return this.json(
      `/studio/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
      body,
    );
  }

  cancel(runId: string): Promise<RunSummary> {
    return this.json<RunSummary>(`/studio/runs/${encodeURIComponent(runId)}/cancel`, {});
  }

  /**
   * Read a run's event stream until it ends or the connection drops.
   *
   * Resolves `"ended"` once `run.end` arrived (the server closes the stream
   * after it) and `"dropped"` when the stream stopped without it: the caller
   * reconnects with the last `seq` it saw. A frame that won't parse is skipped.
   */
  async events(
    runId: string,
    { after, signal, onEvent }: EventsOptions,
  ): Promise<"ended" | "dropped"> {
    const headers = this.headers({ Accept: "text/event-stream" });
    if (after > 0) headers["Last-Event-ID"] = String(after);
    const res = await this.send(`/studio/runs/${encodeURIComponent(runId)}/events`, {
      headers,
      signal: signal ?? null,
    });
    if (!res.ok) throw new ApiError(res.status, await extractError(res), retryAfterSeconds(res));
    if (!res.body) return "dropped";
    let ended = false;
    for await (const frame of readSse(res.body)) {
      let event: StudioEvent;
      try {
        event = JSON.parse(frame.data) as StudioEvent;
      } catch {
        continue;
      }
      if (!event || typeof event !== "object" || typeof event.type !== "string") continue;
      onEvent(event);
      if (event.type === "run.end") ended = true;
    }
    return ended ? "ended" : "dropped";
  }
}
