/**
 * Settings the server provides at runtime (`/console-config.js`, loaded by
 * index.html before this module). They win over the build-time `VITE_*`
 * values, so one published image can be pointed at any identity provider.
 */
const runtime: OrreryRuntimeConfig =
  (typeof window !== "undefined" && window.__ORRERY_CONSOLE_CONFIG__) || {};

/**
 * Runtime configuration, resolved once at module load.
 *
 * The console is served same-origin by the FastAPI front door in production,
 * so the API base defaults to "" (relative URLs hit the serving host). A full
 * URL is only needed for cross-origin dev.
 */
export const config = {
  /** Base URL for API calls; "" means same-origin. Never has a trailing slash. */
  apiBaseUrl: (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, ""),
  /**
   * Claim carrying the caller's roles, as a dotted path. Must match the
   * server's `JWT_ROLE_CLAIM` or the badge disagrees with enforced RBAC.
   * Keycloak's realm roles live at `realm_access.roles`.
   */
  roleClaim: runtime.roleClaim || import.meta.env.VITE_OIDC_ROLE_CLAIM || "roles",
} as const;

/**
 * OIDC single sign-on. Configured only when an issuer is set; otherwise the
 * console falls back to the paste-a-token gate, which keeps `make dev-token`,
 * CI, and offline work usable with no identity provider running.
 */
export const oidcConfig = {
  issuer: (runtime.oidc?.issuer ?? import.meta.env.VITE_OIDC_ISSUER ?? "").replace(/\/+$/, ""),
  clientId: runtime.oidc?.clientId ?? import.meta.env.VITE_OIDC_CLIENT_ID ?? "orrery-console",
  scope: runtime.oidc?.scope ?? import.meta.env.VITE_OIDC_SCOPE ?? "openid profile email",
} as const;

/** True when SSO is configured; drives which sign-in surface the app renders. */
export const isOidcEnabled = oidcConfig.issuer.length > 0;

/** localStorage keys — namespaced to avoid collisions with other apps on the host.
 *
 * Only the token mode's bearer token is stored. Conversation history is not:
 * it lives in the session store and is fetched per browser session, so a shared
 * machine holds no transcripts on disk and no thread is capped to fit a quota.
 */
export const storageKeys = {
  token: "orrery.console.token",
} as const;

/** Keys earlier builds wrote and this one doesn't. Purged on load (see
 * `useConversations`) and on sign-out, so an upgraded browser stops carrying a
 * stale copy of past conversations around. */
export const legacyStorageKeys = [
  "orrery.console.sessionId",
  "orrery.console.conversations",
  "orrery.console.activeConversation",
] as const;
