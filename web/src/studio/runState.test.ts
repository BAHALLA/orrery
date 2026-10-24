import { describe, expect, it } from "vitest";
import { releaseNotesRun } from "./fixtures/events";
import {
  applySummary,
  budgetUse,
  markLost,
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

  it("pairs a reply that arrives late, after other events, with the oldest open request", () => {
    const run = releaseNotesRun();
    const out = run.find((e) => e.type === "a2a.message" && e.direction === "outbound")!;
    const reply = run.find((e) => e.type === "a2a.message" && e.direction === "inbound")!;
    const rest = run.filter((e) => e !== out && e !== reply);
    const exitAt = rest.findIndex((e) => e.type === "node.exit" && e.node_id === "writer");
    const second = { ...out, seq: 101 };
    const mcp = {
      ...run.find((e) => e.type === "mcp.call")!,
      seq: 102,
      node_id: "writer",
    } as StudioEvent;
    // request, second request, an MCP call, then the reply, then the exit.
    const events = [
      ...rest.slice(0, exitAt),
      out,
      second,
      mcp,
      { ...reply, seq: 103 },
      ...rest.slice(exitAt).map((e, i) => ({ ...e, seq: 200 + i }) as StudioEvent),
    ];
    const view = fold(events);
    expect(view.a2a).toHaveLength(2);
    expect(view.a2a[0]!.outbound?.seq).toBe(out.seq);
    expect(view.a2a[0]!.inbound?.seq).toBe(103);
    expect(view.a2a[1]!.inbound).toBeNull();
    expect(view.nodes.writer).toMatchObject({ status: "ok", a2aCalls: 2 });
  });

  it("copes with events that carry no content (past the capture cap)", () => {
    const view = fold(
      releaseNotesRun().map((e) => {
        const copy = { ...e } as Record<string, unknown>;
        delete copy.content;
        return copy as unknown as StudioEvent;
      }),
    );
    expect(view.status).toBe("succeeded");
    expect(view.output).toBeNull();
    expect(view.nodes.critic?.content).toBeNull();
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

  it("ignores everything but a missing run.end once the run is over", () => {
    const run = releaseNotesRun();
    const done = fold(run);
    const late = [
      { ...run[1]!, seq: 50 },
      { ...(run.find((e) => e.type === "budget.hit") ?? run[2]!), seq: 51 },
      { ...run.at(-1)!, seq: 52 },
    ] as StudioEvent[];
    expect(fold(late, done)).toBe(done);
  });

  it("takes an outcome and totals from the summary, then still accepts the run.end", () => {
    const run = releaseNotesRun();
    const partial = fold(run.slice(0, 5));
    expect(partial.active).toBe("writer");
    const summary = {
      run_id: "run-1",
      status: "cancelled",
      spec_name: "release-notes",
      spec_digest: "x",
      created_at: 100,
      ended_at: 103,
      error: "cancelled by alice",
      totals: null,
      event_count: 5,
      dropped_events: 0,
    } as const;
    const settled = applySummary(partial, summary);
    expect(settled).toMatchObject({
      status: "cancelled",
      active: null,
      error: "cancelled by alice",
    });
    expect(settled.nodes.writer?.status).toBe("cancelled");
    expect(isFinished(settled)).toBe(true);
    expect(applySummary(partial, { ...summary, status: "running" })).toBe(partial);
    // Totals missing from the summary still come with the run.end, and nothing else does.
    const ended = reduceRun(settled, run.at(-1)!);
    expect(ended.totals).not.toBeNull();
    expect(reduceRun(settled, { ...run[3]!, seq: 99 } as StudioEvent)).toBe(settled);
  });

  it("marks a run it can no longer read as lost, and its running node as unknown", () => {
    const partial = fold(releaseNotesRun().slice(0, 5));
    const lost = markLost(partial);
    expect(lost.status).toBe("lost");
    expect(isFinished(lost)).toBe(true);
    expect(lost.nodes.writer?.status).toBe("unknown");
    expect(lost.nodes.research?.status).toBe("ok");
    expect(markLost(fold(releaseNotesRun()))).toEqual(fold(releaseNotesRun()));
  });
});
