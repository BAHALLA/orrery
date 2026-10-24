import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { describeApiError } from "../system/useSystem";
import { SpecRefused, type StudioClient } from "./api";
import { MemoryHistory, type RunRecord } from "./history";
import { initialRun, isFinished, reduceRun, type RunView } from "./runState";
import type { CompositionSpec, StudioEvent } from "./types";
import { fromServer, type Issue } from "./validate";

/** Reconnect delays after a dropped stream, ms; a received event resets the count. */
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];

export type Connection = "idle" | "live" | "reconnecting" | "lost";

export interface StudioRunController {
  /** The run on show: the live one, or one picked from history. */
  view: RunView | null;
  connection: Connection;
  error: string | null;
  /** True from the POST until `run.end`. */
  busy: boolean;
  history: RunRecord[];
  /** Start a run. Resolves to the server's field errors on a 422, else null. */
  start: (spec: CompositionSpec, input: string) => Promise<Issue[] | null>;
  cancel: () => Promise<void>;
  /** Show a past run of this session (its graph state, timeline and totals). */
  show: (runId: string) => void;
  clearError: () => void;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

/**
 * Start a Studio run and follow its event stream.
 *
 * The stream is a GET that ends after `run.end`. When it stops early (a proxy
 * timeout, a network blip) it is reopened with `Last-Event-ID` set to the last
 * `seq` seen, so the server replays only what was missed; {@link reduceRun}
 * drops any duplicate. A 404 means the run is gone (finished more than the
 * retention ago, or started on another replica) and is not retried.
 */
export function useStudioRun(client: StudioClient): StudioRunController {
  const [history, setHistory] = useState(() => new MemoryHistory());
  const [shownId, setShownId] = useState<string | null>(null);
  const [liveId, setLiveId] = useState<string | null>(null);
  const [connection, setConnection] = useState<Connection>("idle");
  const [error, setError] = useState<string | null>(null);
  const [posting, setPosting] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const lastSeq = useRef(0);

  useEffect(() => () => abort.current?.abort(), []);

  const apply = useCallback((runId: string, event: StudioEvent) => {
    if (typeof event.seq === "number") lastSeq.current = Math.max(lastSeq.current, event.seq);
    setHistory((h) => {
      const record = h.list().find((r) => r.runId === runId);
      return record ? h.put({ ...record, view: reduceRun(record.view, event) }) : h;
    });
  }, []);

  const follow = useCallback(
    async (runId: string, signal: AbortSignal) => {
      let attempt = 0;
      for (;;) {
        try {
          const result = await client.events(runId, {
            after: lastSeq.current,
            signal,
            onEvent: (event) => {
              attempt = 0;
              setConnection("live");
              apply(runId, event);
            },
          });
          if (result === "ended") {
            setConnection("idle");
            return;
          }
        } catch (err) {
          if (signal.aborted) return;
          if (err instanceof ApiError && (err.status === 404 || err.isAuth)) {
            setConnection("lost");
            setError(
              err.status === 404
                ? "This run is gone: it ended over an hour ago, or it lives on another replica."
                : describeApiError(err, "Not allowed to read this run."),
            );
            return;
          }
        }
        if (signal.aborted) return;
        if (attempt >= BACKOFF_MS.length) {
          setConnection("lost");
          setError("Lost the run's event stream. The run may still be going on the server.");
          return;
        }
        setConnection("reconnecting");
        await sleep(BACKOFF_MS[attempt] ?? 8000, signal);
        attempt += 1;
        if (signal.aborted) return;
      }
    },
    [client, apply],
  );

  const start = useCallback(
    async (spec: CompositionSpec, input: string): Promise<Issue[] | null> => {
      setError(null);
      setPosting(true);
      let runId: string;
      try {
        ({ run_id: runId } = await client.startRun(spec, input));
      } catch (err) {
        setPosting(false);
        if (err instanceof SpecRefused) return fromServer(err.errors);
        setError(describeApiError(err, "The run could not start."));
        return null;
      }
      setPosting(false);
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      lastSeq.current = 0;
      setHistory((h) =>
        h.put({ runId, spec, input, startedAt: Date.now(), view: initialRun(runId) }),
      );
      setLiveId(runId);
      setShownId(runId);
      setConnection("live");
      void follow(runId, controller.signal);
      return null;
    },
    [client, follow],
  );

  const records = history.list();
  const live = records.find((r) => r.runId === liveId)?.view ?? null;
  const view = records.find((r) => r.runId === shownId)?.view ?? null;
  const busy = posting || (live !== null && !isFinished(live) && connection !== "lost");

  const cancel = useCallback(async () => {
    if (!liveId) return;
    try {
      await client.cancel(liveId);
    } catch (err) {
      setError(describeApiError(err, "The run could not be cancelled."));
    }
  }, [client, liveId]);

  const show = useCallback((runId: string) => setShownId(runId), []);
  const clearError = useCallback(() => setError(null), []);

  return useMemo(
    () => ({ view, connection, error, busy, history: records, start, cancel, show, clearError }),
    [view, connection, error, busy, records, start, cancel, show, clearError],
  );
}
