import { describe, expect, it } from "vitest";
import { releaseNotesRun } from "./fixtures/events";
import {
  budgetUse,
  initialRun,
  isFinished,
  reduceRun,
  tokensSoFar,
  type RunView,
} from "./runState";
import type { StudioEvent } from "./types";

const fold = (events: StudioEvent[], from: RunView = initialRun("run-1")) =>
  events.reduce(reduceRun, from);

describe("reduceRun", () => {
  it("plays a whole run: nodes in order, tokens, latency, calls, totals", () => {
    const view = fold(releaseNotesRun());
    expect(view.status).toBe("succeeded");
    expect(isFinished(view)).toBe(true);
    expect(view.order).toEqual(["research", "writer", "critic"]);
    expect(view.nodes.research).toMatchObject({ status: "ok", elapsedMs: 1300, mcpCalls: 1 });
    expect(view.nodes.writer).toMatchObject({ status: "ok", a2aCalls: 1, tokens: { total: 500 } });
    expect(view.totals?.wall_ms).toBe(4300);
    expect(view.lastSeq).toBe(11);
    expect(view.active).toBeNull();
  });

  it("marks the node that is running, and only that one", () => {
    const view = fold(releaseNotesRun().slice(0, 5));
    expect(view.status).toBe("running");
    expect(view.active).toBe("writer");
    expect(view.nodes.writer?.status).toBe("active");
    expect(view.nodes.critic?.status).toBe("pending");
    expect(tokensSoFar(view).total).toBe(1200);
  });

  it("pairs a remote call's request with its reply", () => {
    const view = fold(releaseNotesRun());
    expect(view.a2a).toHaveLength(1);
    expect(view.a2a[0]).toMatchObject({
      nodeId: "writer",
      remote: "demo-writer",
      outbound: { direction: "outbound", seq: 6 },
      inbound: { states: ["submitted", "working", "completed"] },
    });
  });

  it("is idempotent per seq, so a resumed stream that replays an event changes nothing", () => {
    const events = releaseNotesRun();
    const once = fold(events.slice(0, 6));
    const replayed = fold([events[4]!, events[5]!], once);
    expect(replayed).toBe(once);
  });

  it("keeps an unknown event in the timeline and ignores it otherwise", () => {
    const odd = { seq: 2, run_id: "run-1", ts: 1, type: "node.retry" } as unknown as StudioEvent;
    const view = fold([releaseNotesRun()[0]!, odd]);
    expect(view.events).toHaveLength(2);
    expect(view.nodes.research?.status).toBe("pending");
  });

  it("records a budget hit and ends the node that was running with the run", () => {
    const events = releaseNotesRun().slice(0, 5);
    const view = fold([
      ...events,
      {
        seq: 6,
        run_id: "run-1",
        ts: 102,
        type: "budget.hit",
        budget: "max_tool_calls",
        limit: 1,
        used: 1,
      },
      {
        seq: 7,
        run_id: "run-1",
        ts: 102.1,
        type: "run.end",
        outcome: "budget_exhausted",
        error: "max_tool_calls",
        totals: {
          tokens: { input: 1, output: 1, total: 2 },
          tokens_by_model: {},
          tool_calls: 1,
          delegations: 0,
          remote_calls: 0,
          wall_ms: 2000,
        },
      },
    ]);
    expect(view.budgetHit).toMatchObject({ budget: "max_tool_calls", used: 1, limit: 1 });
    expect(view.status).toBe("budget_exhausted");
    expect(view.nodes.writer?.status).toBe("budget_exhausted");
    expect(budgetUse(view, 0).find((b) => b.name === "max_tool_calls")).toEqual({
      name: "max_tool_calls",
      used: 1,
      limit: 12,
    });
  });

  it("estimates budget use from the events while the run is live", () => {
    const view = fold(releaseNotesRun().slice(0, 7));
    const use = Object.fromEntries(budgetUse(view, 103).map((b) => [b.name, b.used]));
    expect(use).toEqual({ max_tokens: 1200, max_tool_calls: 2, max_remote_calls: 1, timeout_s: 3 });
  });
});
