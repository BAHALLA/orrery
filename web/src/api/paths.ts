/**
 * Every URL prefix the console requests from the agent front door.
 *
 * Single source of truth, imported by both {@link ApiClient} (which builds its
 * URLs beneath these) and `vite.config.ts` (which turns them into dev-server
 * proxy entries). They must not drift: in dev the console is served by Vite on
 * :5173 while the API runs on :8000, so an unproxied path never reaches the
 * server. It fails in two different ways depending on the verb, which is what
 * makes the drift hard to spot — a GET is rewritten to the SPA shell and
 * "succeeds" with HTML, while a POST plainly 404s.
 *
 * `client.test.ts` exercises every ApiClient method and asserts its URL starts
 * with one of these, so adding an endpoint without proxying it fails CI rather
 * than silently breaking dev mode.
 */
export const API_PREFIXES = [
  "/chat",
  // Both are needed: the prefix match is exact-or-followed-by-slash, so
  // "/session" does not cover the "/sessions" collection.
  "/sessions",
  "/session",
  "/confirmations",
  "/healthz",
  "/readyz",
  "/me",
  "/onboarding",
  "/admin",
  // Orrery Studio (AEP-031); mounted only when ORRERY_STUDIO_ENABLED.
  "/studio",
  // The learning sandbox's hint ladder (AEP-032); mounted only in learning mode.
  "/learning",
] as const;
