import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { describeApiError } from "../system/useSystem";
import { SpecRefused, type StudioClient } from "./api";
import { MemoryHistory, type RunRecord } from "./history";
import {
  applySummary,
  initialRun,
  isFinished,
  markLost,
  reduceRun,
  type RunView,
} from "./runState";
import type { CompositionSpec, RunSummary, StudioEvent } from "./types";
import { fromServer, type Issue } from "./validate";

/** Reconnect delays after a dropped stream, ms; a received event resets the count. */
export const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];

export type Connection = "idle" | "live" | "reconnecting" | "lost";

export interface StudioRunController {
  /** The run on show: the live one, or one picked from history. */
  view: RunView | null;
  /** The run started last in this view, which `connection` and `cancel` are about. */
  liveRunId: string | null;
  /** The live run's stream. */
  connection: Connection;
  error: string | null;
  /**
   * True from the POST until the live run is known to be over: its `run.end`,
   * or its summary once the stream is lost. A lost stream alone does not end
   * it, since the run may still be going on the server.
   */
  busy: boolean;
  /** A cancel is on its way (or waiting for the run id, when asked during the POST). */
  cancelling: boolean;
  history: RunRecord[];
  /** Start a run. Resolves to the server's field errors on a 422, else null. */
  start: (spec: CompositionSpec, input: string) => Promise<Issue[] | null>;
  cancel: () => Promise<void>;
  /** Follow the live run's stream again, from the last event seen. */
  reconnect: () => void;
  /** Show a past run of this session (its graph state, timeline and totals). */
  show: (runId: string) => void;
  clearError: () => void;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}

/**
 * Start a Studio run and follow its event stream.
 *
 * The stream is a GET that ends after `run.end`. When it stops early (a proxy
 * timeout, a network blip) it is reopened with `Last-Event-ID` set to the last
 * `seq` seen for that run, so the server replays only what was missed;
 * {@link reduceRun} drops any duplicate. When reconnecting gives up, or the
 * stream answers 404, the run's summary (`GET /studio/runs/{id}`) says whether
 * it ended; a run that is still going keeps the canvas locked, with Cancel and
 * Reconnect, and a run that can't be read at all ends as `lost`.
 *
 * Everything that follows a stream is keyed by run id: an old loop winding
 * down never touches the next run's connection state.
 */
export function useStudioRun(client: StudioClient): StudioRunController {
  const [history, setHistory] = useState(() => new MemoryHistory());
  const [shownId, setShownId] = useState<string | null>(null);
  const [liveId, setLiveId] = useState<string | null>(null);
  const [connection, setConnection] = useState<Connection>("idle");
  const [error, setError] = useState<string | null>(null);
  const [posting, setPosting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  /** The run being followed (or about to be): only its loop may set `connection`. */
  const liveRef = useRef<string | null>(null);
  const postingRef = useRef(false);
  const pendingCancel = useRef(false);
  /** The run whose stream a loop is reading now, if any. */
  const following = useRef<string | null>(null);
  /** The last `seq` seen, per run. */
  const seqs = useRef(new Map<string, number>());

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      abort.current?.abort();
    };
  }, []);

  const update = useCallback((runId: string, change: (view: RunView) => RunView) => {
    setHistory((h) => {
      const record = h.list().find((r) => r.runId === runId);
      return record ? h.put({ ...record, view: change(record.view) }) : h;
    });
  }, []);

  const apply = useCallback(
    (runId: string, event: StudioEvent) => {
      if (typeof event.seq === "number") {
        seqs.current.set(runId, Math.max(seqs.current.get(runId) ?? 0, event.seq));
      }
      update(runId, (view) => reduceRun(view, event));
    },
    [update],
  );

  const connectionOf = useCallback((runId: string, value: Connection) => {
    if (liveRef.current === runId) setConnection(value);
  }, []);

  /** Ask the server how a run whose stream is gone stands, and fold the answer in. */
  const settle = useCallback(
    async (runId: string, signal: AbortSignal, why: string) => {
      let summary: RunSummary;
      try {
        summary = await client.run(runId, signal);
      } catch (err) {
        if (signal.aborted) return;
        if (err instanceof ApiError && (err.status === 404 || err.isAuth)) {
          update(runId, markLost);
          connectionOf(runId, "idle");
          if (liveRef.current === runId) {
            setError(
              err.status === 404
                ? "This run is gone: it ended over an hour ago, or it lives on another replica. How it ended is not known."
                : describeApiError(err, "Not allowed to read this run."),
            );
          }
          return;
        }
        // Unreachable for now: the run may still be going. Cancel and Reconnect stay.
        connectionOf(runId, "lost");
        if (liveRef.current === runId) setError(why);
        return;
      }
      if (signal.aborted) return;
      update(runId, (view) => applySummary(view, summary));
      if (summary.status === "running") {
        connectionOf(runId, "lost");
        if (liveRef.current === runId) {
          setError(`${why} The run is still going on the server: reconnect, or cancel it.`);
        }
      } else {
        connectionOf(runId, "idle");
        if (liveRef.current === runId) {
          setError(
            "The event stream was lost; the outcome and totals come from the run's summary.",
          );
        }
      }
    },
    [client, update, connectionOf],
  );

  const follow = useCallback(
    async (runId: string, signal: AbortSignal) => {
      let attempt = 0;
      for (;;) {
        try {
          const result = await client.events(runId, {
            after: seqs.current.get(runId) ?? 0,
            signal,
            onEvent: (event) => {
              attempt = 0;
              connectionOf(runId, "live");
              apply(runId, event);
            },
          });
          if (result === "ended") {
            connectionOf(runId, "idle");
            return;
          }
        } catch (err) {
          if (signal.aborted) return;
          if (err instanceof ApiError && (err.status === 404 || err.isAuth)) {
            await settle(runId, signal, "The run's event stream is gone.");
            return;
          }
        }
        if (signal.aborted) return;
        if (attempt >= BACKOFF_MS.length) {
          await settle(runId, signal, "Lost the run's event stream.");
          return;
        }
        connectionOf(runId, "reconnecting");
        await sleep(BACKOFF_MS[attempt] ?? 8000, signal);
        attempt += 1;
        if (signal.aborted) return;
      }
    },
    [client, apply, settle, connectionOf],
  );

  const followAnew = useCallback(
    (runId: string) => {
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      following.current = runId;
      void follow(runId, controller.signal).finally(() => {
        if (abort.current === controller) following.current = null;
      });
    },
    [follow],
  );

  const sendCancel = useCallback(
    async (runId: string) => {
      setCancelling(true);
      try {
        const summary = await client.cancel(runId);
        update(runId, (view) => applySummary(view, summary));
        // Cancelling is asynchronous: when the stream was lost, pick it up again
        // to see the run end.
        if (
          mounted.current &&
          summary.status === "running" &&
          liveRef.current === runId &&
          following.current !== runId
        ) {
          followAnew(runId);
        }
      } catch (err) {
        if (mounted.current) setError(describeApiError(err, "The run could not be cancelled."));
      } finally {
        if (mounted.current) setCancelling(false);
      }
    },
    [client, update, followAnew],
  );

  const start = useCallback(
    async (spec: CompositionSpec, input: string): Promise<Issue[] | null> => {
      setError(null);
      setPosting(true);
      postingRef.current = true;
      pendingCancel.current = false;
      let runId: string;
      try {
        ({ run_id: runId } = await client.startRun(spec, input));
      } catch (err) {
        postingRef.current = false;
        pendingCancel.current = false;
        if (!mounted.current) return null;
        setPosting(false);
        setCancelling(false);
        if (err instanceof SpecRefused) return fromServer(err.errors);
        setError(describeApiError(err, "The run could not start."));
        return null;
      }
      postingRef.current = false;
      // Gone while the POST was out: nothing to follow, nothing to show.
      if (!mounted.current) return null;
      setPosting(false);
      liveRef.current = runId;
      seqs.current.set(runId, 0);
      setHistory((h) =>
        h.put({ runId, spec, input, startedAt: Date.now(), view: initialRun(runId) }),
      );
      setLiveId(runId);
      setShownId(runId);
      setConnection("live");
      followAnew(runId);
      if (pendingCancel.current) {
        pendingCancel.current = false;
        void sendCancel(runId);
      }
      return null;
    },
    [client, followAnew, sendCancel],
  );

  const records = history.list();
  const live = records.find((r) => r.runId === liveId)?.view ?? null;
  const view = records.find((r) => r.runId === shownId)?.view ?? null;
  const busy = posting || (live !== null && !isFinished(live));

  const cancel = useCallback(async () => {
    // Asked before the run has an id: the cancel goes out as soon as it has one.
    if (postingRef.current) {
      pendingCancel.current = true;
      setCancelling(true);
      return;
    }
    const runId = liveRef.current;
    if (runId) await sendCancel(runId);
  }, [sendCancel]);

  const reconnect = useCallback(() => {
    const runId = liveRef.current;
    if (!runId) return;
    setError(null);
    setConnection("reconnecting");
    followAnew(runId);
  }, [followAnew]);

  const show = useCallback((runId: string) => setShownId(runId), []);
  const clearError = useCallback(() => setError(null), []);

  return useMemo(
    () => ({
      view,
      liveRunId: liveId,
      connection,
      error,
      busy,
      cancelling,
      history: records,
      start,
      cancel,
      reconnect,
      show,
      clearError,
    }),
    [
      view,
      liveId,
      connection,
      error,
      busy,
      cancelling,
      records,
      start,
      cancel,
      reconnect,
      show,
      clearError,
    ],
  );
}
