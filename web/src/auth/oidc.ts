import { InMemoryWebStorage, type User, UserManager, WebStorageStateStore } from "oidc-client-ts";
import { isOidcEnabled, oidcConfig, storageKeys } from "../config";

/**
 * OIDC single sign-on for the console (Authorization Code + PKCE).
 *
 * Provider-agnostic — the same configuration works against Keycloak, Authentik,
 * Auth0, Okta, Entra ID, or Google. PKCE with a public client is the correct
 * pattern for a browser SPA: there is nowhere to keep a client secret, and the
 * code challenge is what stops an intercepted authorization code being redeemed
 * by anyone else.
 *
 * Two deliberate choices:
 *
 * - **`redirect_uri` is the console root**, not a sub-path. The FastAPI front
 *   door serves the bundle with `StaticFiles(html=True)` (server.py), which
 *   404s unknown deep paths — a `/auth/callback` route would work under the
 *   Vite dev server and break in production. Returning to `/` with the code and
 *   state as query params needs no SPA router and no server change.
 * - **Tokens live in memory only.** oidc-client-ts's user record holds the
 *   access, ID and refresh tokens, and its default store is sessionStorage, where
 *   any script on the origin can read them. So a reload loses them, and
 *   {@link restoreSession} gets new ones from the provider's own session with a
 *   `prompt=none` redirect. That is a top-level navigation rather than a hidden
 *   iframe, because browsers block the provider's cookies in an iframe.
 */

/** Where the provider sends the browser back to. Always the console root. */
export function redirectUri(): string {
  return `${window.location.origin}/`;
}

let manager: UserManager | null = null;

/** The shared UserManager, or null when SSO isn't configured. */
export function getUserManager(): UserManager | null {
  if (!isOidcEnabled) return null;
  if (manager) return manager;

  manager = new UserManager({
    authority: oidcConfig.issuer,
    client_id: oidcConfig.clientId,
    redirect_uri: redirectUri(),
    post_logout_redirect_uri: redirectUri(),
    response_type: "code",
    scope: oidcConfig.scope,
    // Renew in the background so a long incident session doesn't hit a login
    // wall mid-investigation.
    automaticSilentRenew: true,
    // The user record holds the tokens: memory only.
    userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
    // One redirect's `state` and PKCE verifier, which must survive it and are
    // removed when it completes. No token.
    stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
  });
  return manager;
}

/** True when the current URL carries an authorization response. */
export function hasAuthResponse(search: string = window.location.search): boolean {
  const params = new URLSearchParams(search);
  return params.has("code") || params.has("error");
}

/**
 * Complete a redirect sign-in and strip the code/state from the address bar.
 *
 * The parameters are removed with `replaceState` so a refresh doesn't attempt
 * to redeem an already-used authorization code (which the provider rejects) and
 * so the code never lingers in browser history.
 */
export async function completeSignIn(): Promise<User | null> {
  const um = getUserManager();
  if (!um) return null;
  try {
    return await um.signinRedirectCallback();
  } finally {
    window.history.replaceState({}, document.title, window.location.pathname);
  }
}

/** Note that this tab is signed in, so a reload may try to restore the session. */
export function rememberSession(): void {
  try {
    window.sessionStorage.setItem(storageKeys.oidcSession, "1");
  } catch {
    // Storage disabled: a reload shows the sign-in panel instead.
  }
}

export function forgetSession(): void {
  try {
    window.sessionStorage.removeItem(storageKeys.oidcSession);
  } catch {
    // Storage disabled: nothing was remembered.
  }
}

/** Whether this tab was signed in. Clears the note, so one reload tries at most once. */
function takeRememberedSession(): boolean {
  try {
    const remembered = window.sessionStorage.getItem(storageKeys.oidcSession) !== null;
    forgetSession();
    return remembered;
  } catch {
    return false;
  }
}

/**
 * The user held in memory, or a new sign-in if this tab had one before a reload.
 *
 * Asking with `prompt=none` shows no provider page: it comes back with a code,
 * or with `login_required` when the provider's session has ended. Nothing else
 * starts a redirect, so a tab that never signed in shows the sign-in panel.
 */
export async function restoreSession(): Promise<User | null> {
  const um = getUserManager();
  if (!um) return null;
  const user = await um.getUser();
  if (user && !user.expired) return user;
  if (takeRememberedSession()) await um.signinRedirect({ prompt: "none" });
  return null;
}

export async function signInRedirect(): Promise<void> {
  await getUserManager()?.signinRedirect();
}

/** Sign out of the provider too — a local-only sign-out silently signs back in. */
export async function signOutRedirect(): Promise<void> {
  forgetSession();
  const um = getUserManager();
  if (!um) return;
  try {
    await um.signoutRedirect();
  } catch {
    // Providers without an end-session endpoint (or with it disabled) reject
    // this. Dropping the local session is still the right outcome.
    await um.removeUser();
  }
}
