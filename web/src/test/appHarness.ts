/**
 * Shared setup for the App-level tests (`App.*.test.tsx`): canned responses,
 * SSE frames, a display-only JWT and a fetch stub routing each endpoint.
 */
import { vi } from "vitest";

/** A minimal unsigned JWT for the given claims (display decoding only). */
export function makeToken(payload: Record<string, unknown>): string {
  const b64 = (obj: unknown) =>
    btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.sig`;
}

/** A 200 JSON response. */
export function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/** One `/chat/stream` frame. */
export function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** A `progress` frame's step: a root-level tool start unless `over` says otherwise. */
export function progressStep(over: Record<string, unknown>) {
  return {
    call_id: "c",
    phase: "start",
    agent: "orrery_chat_agent",
    tool: "t",
    delegation: false,
    guarded: false,
    outcome: null,
    status: "",
    elapsed_ms: null,
    ...over,
  };
}

/** A complete `text/event-stream` response made of `frames`. */
export function sseResponse(frames: string[]): Response {
  return new Response(frames.join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/**
 * Route each endpoint to a canned response; /chat is caller-controlled.
 * `me` replaces the default operator's `/me` (autonomy L3, self-test on).
 */
export function stubApi(chat: () => Promise<Response>, me?: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/chat")) return chat();
      if (url.endsWith("/me")) {
        return jsonResponse(
          me ?? {
            subject: "alice",
            role: "operator",
            autonomy_level: "L3",
            model_provider: "gemini",
            model_name: "gemini-3-pro",
            self_test_available: true,
          },
        );
      }
      if (url.endsWith("/admin/agents")) {
        return jsonResponse({
          agents: [
            {
              name: "k8s_health_agent",
              description: "Kubernetes",
              source: "builtin",
              tools: [
                { name: "list_pods", guard: "read", reason: "" },
                { name: "delete_pod", guard: "destructive", reason: "deletes a pod" },
              ],
            },
            {
              name: "github_mcp_agent",
              description: "GitHub",
              source: "mcp",
              tools: [{ name: "github_*", guard: "dynamic", reason: "" }],
            },
          ],
        });
      }
      if (url.includes("/onboarding/selftest")) {
        return jsonResponse({
          ok: false,
          checks: [
            {
              name: "model",
              label: "gemini / gemini-3-pro",
              ok: true,
              detail: "Reached gemini.",
              hint: "",
              duration_ms: 120,
            },
            {
              name: "kafka",
              label: "Kafka",
              ok: false,
              detail: "Connection refused to broker-1:9092",
              hint: "Set KAFKA_BOOTSTRAP_SERVERS to a reachable broker list.",
              duration_ms: 40,
            },
          ],
        });
      }
      return jsonResponse({ entries: [], pending: null, severity: null, report: null });
    }),
  );
}

/** Undo a test's fetch stub and spies, and forget its token. */
export function restoreGlobals() {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
}
