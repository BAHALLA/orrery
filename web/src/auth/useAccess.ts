import { useEffect, useRef, useState } from "react";
import { ApiClient, ApiError } from "../api/client";
import { config } from "../config";

export type Access = "checking" | "granted" | "denied";

/**
 * On a deployment with `ORRERY_REQUIRED_ROLES`, asks the server once per
 * person whether they may use the deployment at all, so a refused person sees
 * one clear page and the console never starts loading.
 *
 * Keyed by who is signed in, not by the token: an SSO silent renew replaces
 * the token every few minutes, and re-checking on each one would unmount the
 * console (and the turn on screen, the draft, the open panels) while `/me`
 * answers. Anything but an `access_denied` answer opens the console, whose own
 * error handling (expired token, server down) is the right place for it.
 */
export function useAccess(
  who: string | null,
  token: string | null,
  restricted: boolean = config.accessRestricted,
): Access {
  const [state, setState] = useState<{ who: string | null; access: Access }>({
    who: null,
    access: "checking",
  });
  const tokenRef = useRef(token);
  useEffect(() => {
    tokenRef.current = token;
  }, [token]);
  useEffect(() => {
    if (who === null || !restricted) return;
    const controller = new AbortController();
    new ApiClient(tokenRef.current)
      .me({ signal: controller.signal })
      .then(() => setState({ who, access: "granted" }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const denied = err instanceof ApiError && err.isAccessDenied;
        setState({ who, access: denied ? "denied" : "granted" });
      });
    return () => {
      controller.abort();
      // Whoever comes next (after a sign-out, or another account) starts
      // undecided: a verdict cached for this person must not show the console
      // to the next one while their own /me is in flight.
      setState({ who: null, access: "checking" });
    };
  }, [who, restricted]);
  if (!restricted) return "granted";
  return state.who === who ? state.access : "checking";
}
