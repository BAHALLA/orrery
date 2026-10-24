import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { StudioClient } from "./api";
import { releaseNotesRun } from "./fixtures/events";
import { RELEASE_NOTES } from "./templates";
import type { RunSummary, StudioEvent } from "./types";
import { useStudioRun } from "./useStudioRun";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

type EventsArgs = {
  after: number;
  signal?: AbortSignal;
  onEvent: (e: StudioEvent) => void;
};

/** A stream that stays open until the test ends it or the hook aborts it. */
function openStream(opts: EventsArgs, events: StudioEvent[] = []): Promise<"ended" | "dropped"> {
  for (const e of events) opts.onEvent(e);
  return new Promise((resolve) => {
    opts.signal?.addEventListener("abort", () => resolve("dropped"));
  });
}

function summary(runId: string, status: RunSummary["status"]): RunSummary {
  return {
    run_id: runId,
    status,
    spec_name: "release-notes",
    spec_digest: "5815ab12",
    created_at: 100,
    ended_at: status === "running" ? null : 104.3,
    error: null,
    totals:
      status === "running"
        ? null
        : {
            tokens: { input: 1, output: 2, total: 3 },
            tokens_by_model: {},
            tool_calls: 0,
            delegations: 0,
            remote_calls: 0,
            wall_ms: 4300,
          },
    event_count: 3,
    dropped_events: 0,
  };
}

function fakeClient() {
  const client = {
    startRun: vi.fn<(...a: unknown[]) => Promise<{ run_id: string }>>(),
    events: vi.fn<(runId: string, opts: EventsArgs) => Promise<"ended" | "dropped">>(),
    run: vi.fn<(runId: string) => Promise<RunSummary>>(),
    cancel: vi.fn<(runId: string) => Promise<RunSummary>>(),
  };
  return { client, asClient: client as unknown as StudioClient };
}

/** The release-notes run's events, re-stamped for another run id. */
function eventsOf(runId: string): StudioEvent[] {
  return releaseNotesRun().map((e) => ({ ...e, run_id: runId }) as StudioEvent);
}

afterEach(() => vi.useRealTimers());

describe("useStudioRun", () => {
  it("a cancel asked during the POST goes to the new run, not the one before", async () => {
    const { client, asClient } = fakeClient();
    const first = eventsOf("run-a");
    client.events.mockImplementation((runId, opts) =>
      runId === "run-a"
        ? Promise.resolve((first.forEach(opts.onEvent), "ended"))
        : openStream(opts),
    );
    client.startRun.mockResolvedValueOnce({ run_id: "run-a" });
    const { result } = renderHook(() => useStudioRun(asClient));
    await act(() => result.current.start(RELEASE_NOTES, "lag"));
    await waitFor(() => expect(result.current.busy).toBe(false));

    const post = deferred<{ run_id: string }>();
    client.startRun.mockReturnValueOnce(post.promise);
    client.cancel.mockResolvedValue(summary("run-b", "running"));
    let started!: Promise<unknown>;
    act(() => {
      started = result.current.start(RELEASE_NOTES, "lag again");
    });
    expect(result.current.busy).toBe(true);
    await act(() => result.current.cancel());
    expect(result.current.cancelling).toBe(true);
    expect(client.cancel).not.toHaveBeenCalled();

    await act(async () => {
      post.resolve({ run_id: "run-b" });
      await started;
    });
    await waitFor(() => expect(client.cancel).toHaveBeenCalledTimes(1));
    expect(client.cancel).toHaveBeenCalledWith("run-b");
    await waitFor(() => expect(result.current.cancelling).toBe(false));
  });

  it("keeps the run going when its stream is lost and the summary says it still runs", async () => {
    const { client, asClient } = fakeClient();
    const events = eventsOf("run-a");
    let calls = 0;
    client.events.mockImplementation((_runId, opts) => {
      calls += 1;
      if (calls === 1) {
        events.slice(0, 4).forEach(opts.onEvent);
        return Promise.reject(new ApiError(404, "No such run"));
      }
      events.filter((e) => (e.seq ?? 0) > opts.after).forEach(opts.onEvent);
      return Promise.resolve("ended");
    });
    client.run.mockResolvedValue(summary("run-a", "running"));
    client.startRun.mockResolvedValue({ run_id: "run-a" });
    const { result } = renderHook(() => useStudioRun(asClient));
    await act(() => result.current.start(RELEASE_NOTES, "lag"));

    await waitFor(() => expect(result.current.connection).toBe("lost"));
    // Still running on the server: locked, with Cancel and Reconnect.
    expect(result.current.busy).toBe(true);
    expect(result.current.view?.status).toBe("running");
    expect(result.current.error).toMatch(/still going/);

    act(() => result.current.reconnect());
    await waitFor(() => expect(result.current.view?.status).toBe("succeeded"));
    // Resumed after the last event it had, not from the start.
    expect(client.events.mock.calls[1]?.[1].after).toBe(4);
    expect(result.current.busy).toBe(false);
    await waitFor(() => expect(result.current.connection).toBe("idle"));
  });

  it("takes the outcome from the summary when the run ended while the stream was gone", async () => {
    const { client, asClient } = fakeClient();
    client.events.mockImplementation((_runId, opts) => {
      eventsOf("run-a").slice(0, 3).forEach(opts.onEvent);
      return Promise.reject(new ApiError(404, "gone"));
    });
    client.run.mockResolvedValue(summary("run-a", "succeeded"));
    client.startRun.mockResolvedValue({ run_id: "run-a" });
    const { result } = renderHook(() => useStudioRun(asClient));
    await act(() => result.current.start(RELEASE_NOTES, "lag"));

    await waitFor(() => expect(result.current.view?.status).toBe("succeeded"));
    expect(result.current.view?.totals?.wall_ms).toBe(4300);
    expect(result.current.busy).toBe(false);
    expect(result.current.history[0]?.view.status).toBe("succeeded");
  });

  it("ends a run it can no longer read as lost, so history never says running forever", async () => {
    const { client, asClient } = fakeClient();
    client.events.mockImplementation((_runId, opts) => {
      eventsOf("run-a").slice(0, 3).forEach(opts.onEvent);
      return Promise.reject(new ApiError(404, "gone"));
    });
    client.run.mockRejectedValue(new ApiError(404, "gone"));
    client.startRun.mockResolvedValue({ run_id: "run-a" });
    const { result } = renderHook(() => useStudioRun(asClient));
    await act(() => result.current.start(RELEASE_NOTES, "lag"));

    await waitFor(() => expect(result.current.view?.status).toBe("lost"));
    expect(result.current.busy).toBe(false);
    expect(result.current.history[0]?.view.status).toBe("lost");
    expect(result.current.view?.nodes.research?.status).toBe("unknown");
  });

  it("gives up reconnecting after the backoff, then asks for the summary", async () => {
    vi.useFakeTimers();
    const { client, asClient } = fakeClient();
    client.events.mockRejectedValue(new ApiError(0, "Network error"));
    client.run.mockRejectedValue(new ApiError(0, "Network error"));
    client.startRun.mockResolvedValue({ run_id: "run-a" });
    const { result } = renderHook(() => useStudioRun(asClient));
    await act(() => result.current.start(RELEASE_NOTES, "lag"));
    await act(() => vi.advanceTimersByTimeAsync(20_000));

    expect(client.events).toHaveBeenCalledTimes(6);
    expect(client.run).toHaveBeenCalledWith("run-a", expect.anything());
    expect(result.current.connection).toBe("lost");
    // Unreachable is not over: the canvas stays locked, Cancel and Reconnect stay.
    expect(result.current.busy).toBe(true);
  });

  it("keeps each run's stream state apart: an old loop never touches the new run", async () => {
    const { client, asClient } = fakeClient();
    const firstStream = deferred<"ended" | "dropped">();
    client.events.mockImplementation((runId, opts) => {
      if (runId === "run-a") {
        eventsOf("run-a").forEach(opts.onEvent);
        return firstStream.promise;
      }
      opts.onEvent(eventsOf("run-b")[0]!);
      return openStream(opts);
    });
    client.startRun.mockResolvedValueOnce({ run_id: "run-a" });
    const { result } = renderHook(() => useStudioRun(asClient));
    await act(() => result.current.start(RELEASE_NOTES, "lag"));
    await waitFor(() => expect(result.current.busy).toBe(false));

    client.startRun.mockResolvedValueOnce({ run_id: "run-b" });
    await act(() => result.current.start(RELEASE_NOTES, "again"));
    await waitFor(() => expect(result.current.connection).toBe("live"));
    // The new run's stream starts from its own beginning, not run-a's last seq.
    expect(client.events.mock.calls[1]?.[0]).toBe("run-b");
    expect(client.events.mock.calls[1]?.[1].after).toBe(0);

    // run-a's loop winds down late: run-b stays live.
    await act(async () => {
      firstStream.resolve("ended");
      await Promise.resolve();
    });
    expect(result.current.connection).toBe("live");
    expect(result.current.liveRunId).toBe("run-b");
  });

  it("does not follow a run whose POST returns after the view is gone", async () => {
    const { client, asClient } = fakeClient();
    const post = deferred<{ run_id: string }>();
    client.startRun.mockReturnValue(post.promise);
    const { result, unmount } = renderHook(() => useStudioRun(asClient));
    let started!: Promise<unknown>;
    act(() => {
      started = result.current.start(RELEASE_NOTES, "lag");
    });
    unmount();
    post.resolve({ run_id: "run-a" });
    await started;
    expect(client.events).not.toHaveBeenCalled();
  });
});
