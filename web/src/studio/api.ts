import { ApiError, extractError, retryAfterSeconds } from "../api/client";
import { readSse } from "../api/sse";
import { config } from "../config";
import type {
  CompositionSpec,
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
    const res = await this.send(path, {
      method: body === undefined ? "GET" : "POST",
      headers: this.headers(body === undefined ? {} : { "Content-Type": "application/json" }),
      body: body === undefined ? null : JSON.stringify(body),
      signal: signal ?? null,
    });
    if (!res.ok) throw new ApiError(res.status, await extractError(res), retryAfterSeconds(res));
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

  /** Start a run. Throws {@link SpecRefused} with the server's field errors on a 422. */
  async startRun(spec: CompositionSpec, input: string): Promise<{ run_id: string }> {
    const res = await this.send("/studio/runs", {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ spec, input }),
    });
    if (res.status === 422) {
      const errors = specErrors(
        await res
          .clone()
          .json()
          .catch(() => null),
      );
      if (errors) throw new SpecRefused(errors);
    }
    if (!res.ok) throw new ApiError(res.status, await extractError(res), retryAfterSeconds(res));
    return (await res.json()) as { run_id: string };
  }

  run(runId: string, signal?: AbortSignal): Promise<RunSummary> {
    return this.json<RunSummary>(`/studio/runs/${encodeURIComponent(runId)}`, undefined, signal);
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
