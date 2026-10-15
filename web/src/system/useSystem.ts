import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiClient, ApiError } from "../api/client";
import type { InventoryAgent, MeResponse, SelfTestResponse } from "../api/types";

export interface SystemController {
  /** The server's own view of the caller — null until it loads. */
  me: MeResponse | null;
  /** Why `me` is unavailable, when the fetch failed for a reason worth showing. */
  meError: string | null;
  selfTest: SelfTestResponse | null;
  isChecking: boolean;
  error: string | null;
  runSelfTest: () => Promise<void>;
  /** Agent inventory for the admin view; null until loaded. */
  agents: InventoryAgent[] | null;
  agentsError: string | null;
  loadAgents: () => Promise<void>;
}

/** Turn a failure into something that tells the operator what to do about it. */
export function describeApiError(err: unknown, fallback: string): string {
  if (!(err instanceof ApiError)) return fallback;
  if (err.status === 0) return "Can't reach the server — is the API running?";
  if (err.isAuth) return "Your session isn't authorised for this. Try signing in again.";
  if (err.isRateLimited) {
    return `Rate limited${err.retryAfter ? ` — try again in ${err.retryAfter}s` : ", wait a moment"}.`;
  }
  if (err.status >= 500) return `The server failed (${err.status}). Check the API logs.`;
  return err.message || fallback;
}

/**
 * Deployment posture and first-run diagnostics (AEP-019 Milestone 3).
 *
 * `/me` is fetched once on mount: it carries the role the *server* resolved and
 * the autonomy level actually in force, which is what decides whether a
 * mutating tool will run. The console also decodes the JWT locally for the
 * badge, but that is the browser's reading of a token it cannot verify — this
 * is the authoritative answer, and the two are shown together so a mismatch is
 * visible rather than mysterious.
 *
 * The self-test is on demand, never automatic: it reaches out to real
 * infrastructure and does a one-token model round-trip, so it runs when an
 * operator asks for it.
 */
export function useSystem(token: string | null): SystemController {
  const client = useMemo(() => new ApiClient(token), []); // eslint-disable-line react-hooks/exhaustive-deps
  const [me, setMe] = useState<MeResponse | null>(null);
  const [meError, setMeError] = useState<string | null>(null);
  const [selfTest, setSelfTest] = useState<SelfTestResponse | null>(null);
  const [isChecking, setIsChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    client.setToken(token);
  }, [client, token]);

  useEffect(() => {
    let cancelled = false;
    client
      .me()
      .then((res) => {
        if (cancelled) return;
        setMe(res);
        setMeError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setMe(null);
        // An older server genuinely without /me is fine — the posture rows just
        // stay empty. Anything else (unreachable, unauthorised, the dev proxy
        // answering with the SPA shell) used to be swallowed here, leaving the
        // panel showing "—" with no way to tell a missing route from a broken
        // one. That silence is what made the reported bug hard to place.
        setMeError(
          err instanceof ApiError && err.status === 404
            ? null
            : describeApiError(err, "Could not load deployment posture."),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [client, token]);

  const [agents, setAgents] = useState<InventoryAgent[] | null>(null);
  const [agentsError, setAgentsError] = useState<string | null>(null);

  const loadAgents = useCallback(async () => {
    setAgentsError(null);
    try {
      setAgents((await client.agents()).agents);
    } catch (err) {
      setAgentsError(describeApiError(err, "Could not load the agent inventory."));
    }
  }, [client]);

  const runSelfTest = useCallback(async () => {
    if (isChecking) return;
    setIsChecking(true);
    setError(null);
    try {
      setSelfTest(await client.selfTest());
    } catch (err) {
      setError(describeApiError(err, "Could not run the environment check."));
    } finally {
      setIsChecking(false);
    }
  }, [client, isChecking]);

  return {
    me,
    meError,
    selfTest,
    isChecking,
    error,
    runSelfTest,
    agents,
    agentsError,
    loadAgents,
  };
}
