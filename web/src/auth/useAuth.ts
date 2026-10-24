import { useCallback, useEffect, useMemo, useState } from "react";
import { isOidcEnabled, legacyStorageKeys, storageKeys } from "../config";
import {
  completeSignIn,
  getUserManager,
  hasAuthResponse,
  loadUser,
  signInRedirect,
  signOutRedirect,
} from "./oidc";
import { identityFromToken, isExpired, type Identity } from "./token";

export interface AuthState {
  token: string | null;
  identity: Identity | null;
  /** True when a token is present and either has no expiry or hasn't expired. */
  isAuthenticated: boolean;
  /** Which sign-in surface applies: SSO, or the paste-a-token gate. */
  mode: "oidc" | "token";
  /** True while an OIDC session is being restored or a redirect completed. */
  isLoading: boolean;
  /** Sign-in failure worth showing on the gate. */
  error: string | null;
  /** Token mode: accept a pasted bearer token. */
  signIn: (token: string) => void;
  /** OIDC mode: start the Authorization Code + PKCE redirect. */
  signInWithSso: () => void;
  signOut: () => void;
}

export const SESSION_ENDED = "Your sign-in session ended. Sign in again to continue.";

/** How long an expired token may wait for a renew already in flight. */
export const EXPIRY_GRACE_MS = 10_000;

/** OAuth errors that mean the provider no longer honours this session. */
const SESSION_REFUSALS = new Set(["invalid_grant", "login_required", "interaction_required"]);

/** True when a silent-renew failure is the provider refusing, not the network. */
export function isSessionRefusal(err: unknown): boolean {
  const code = (err as { error?: unknown } | null)?.error;
  return typeof code === "string" && SESSION_REFUSALS.has(code);
}

function readStoredToken(): string | null {
  try {
    return localStorage.getItem(storageKeys.token);
  } catch {
    return null; // storage disabled (private mode / SSR)
  }
}

/** Drop everything user-scoped so a shared browser never leaks the previous
 *  user's session to whoever signs in next.
 *
 *  Transcripts are no longer among it — history lives in the session store and
 *  is fetched with the new user's own token, so signing in as someone else can
 *  only ever show that person's conversations. The legacy keys are still cleared
 *  because a browser upgraded from an earlier build may hold a copy on disk. */
function clearLocalState(): void {
  try {
    localStorage.removeItem(storageKeys.token);
    legacyStorageKeys.forEach((k) => localStorage.removeItem(k));
  } catch {
    // ignore
  }
}

/**
 * Owns the caller's bearer token in whichever mode this deployment runs.
 *
 * Both modes end at the same place — a bearer token the server verifies on
 * every request — so the shape below is deliberately identical for each, and
 * the rest of the console never learns which one is in play.
 *
 * - **OIDC** (`VITE_OIDC_ISSUER` set): Authorization Code + PKCE. The access
 *   token stays in memory and is renewed silently.
 * - **Token** (default): the operator pastes a token, e.g. from
 *   `make dev-token`. Kept so CI and offline work need no identity provider.
 */
export function useAuth(): AuthState {
  const [token, setToken] = useState<string | null>(() =>
    isOidcEnabled ? null : readStoredToken(),
  );
  // In OIDC mode nothing can be decided until the session is restored (or a
  // redirect is redeemed); rendering the sign-in panel before then would flash
  // a login screen at an already-authenticated user on every refresh.
  const [isLoading, setIsLoading] = useState(isOidcEnabled);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOidcEnabled) return;
    let cancelled = false;
    // The token these handlers last saw, so a timer can tell whether a renew
    // replaced it while it waited.
    let current: string | null = null;
    let expiryTimer: ReturnType<typeof setTimeout> | undefined;

    void (async () => {
      try {
        const user = hasAuthResponse() ? await completeSignIn() : await loadUser();
        current = user?.access_token ?? null;
        if (!cancelled) setToken(current);
      } catch (err) {
        if (!cancelled) {
          setToken(null);
          setError(err instanceof Error ? err.message : "Single sign-on failed.");
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();

    // Silent renew swaps the access token underneath us; without this the
    // console would keep sending the old one until the next full reload.
    const um = getUserManager();
    const onLoaded = (user: { access_token: string }) => {
      current = user.access_token;
      setToken(current);
      setError(null);
    };
    const onUnloaded = () => {
      current = null;
      setToken(null);
    };
    // The provider can end the session under us (idle timeout, an admin
    // logout, a laptop asleep past the SSO idle limit): the refresh is refused
    // and the access token then expires. Neither raises userUnloaded, so the
    // console kept a dead token and every call failed with 401 until a reload.
    const endSession = () => {
      if (cancelled) return;
      current = null;
      setToken(null);
      setError(SESSION_ENDED);
    };
    const onRenewError = (err: Error) => {
      // A network blip leaves the current token usable until it expires,
      // which onExpired handles; only a refusal means the session is gone,
      // and then its refresh token is useless too.
      if (!isSessionRefusal(err)) return;
      endSession();
      void um?.removeUser();
    };
    const onExpired = () => {
      // Waking from sleep fires the renew and the expiry together, and the
      // renew usually wins a moment later. Wait for it rather than flash the
      // sign-in panel; the stored user stays, so a renew still retrying can
      // bring the session back.
      const expired = current;
      if (expired === null) return; // nothing was signed in (a reload with a stale record)
      clearTimeout(expiryTimer);
      expiryTimer = setTimeout(() => {
        if (current === expired) endSession();
      }, EXPIRY_GRACE_MS);
    };
    um?.events.addUserLoaded(onLoaded);
    um?.events.addUserUnloaded(onUnloaded);
    um?.events.addSilentRenewError(onRenewError);
    um?.events.addAccessTokenExpired(onExpired);

    return () => {
      cancelled = true;
      um?.events.removeUserLoaded(onLoaded);
      um?.events.removeUserUnloaded(onUnloaded);
      um?.events.removeSilentRenewError(onRenewError);
      um?.events.removeAccessTokenExpired(onExpired);
      clearTimeout(expiryTimer);
    };
  }, []);

  const signIn = useCallback((next: string) => {
    const trimmed = next.trim();
    try {
      localStorage.setItem(storageKeys.token, trimmed);
    } catch {
      // Non-persistent session if storage is unavailable — still usable in-memory.
    }
    setToken(trimmed);
  }, []);

  const signInWithSso = useCallback(() => {
    setError(null);
    void signInRedirect().catch((err: unknown) => {
      setError(err instanceof Error ? err.message : "Could not reach the identity provider.");
    });
  }, []);

  const signOut = useCallback(() => {
    clearLocalState();
    setToken(null);
    // Ending the provider session too — otherwise "sign out" followed by
    // "sign in" silently re-authenticates the same user with no prompt, which
    // is not what anyone means by signing out of a shared machine.
    if (isOidcEnabled) void signOutRedirect();
  }, []);

  const identity = useMemo(() => (token ? identityFromToken(token) : null), [token]);

  const isAuthenticated = useMemo(
    () => token !== null && (identity === null || !isExpired(identity)),
    [token, identity],
  );

  return {
    token,
    identity,
    isAuthenticated,
    mode: isOidcEnabled ? "oidc" : "token",
    isLoading,
    error,
    signIn,
    signInWithSso,
    signOut,
  };
}
