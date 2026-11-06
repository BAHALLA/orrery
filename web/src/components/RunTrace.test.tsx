import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { Trace, TraceCall } from "../chat/trace";
import { RunTrace } from "./RunTrace";

const call = (state: TraceCall["state"]): TraceCall =>
  ({
    callId: "1",
    tool: "reset_consumer_group_offsets",
    agent: "kafka_health_agent",
    state,
    delegation: false,
    guarded: true,
    status: state === "refused" ? "BLOCKED" : "success",
    elapsedMs: 10,
    children: [],
  }) as TraceCall;

const details = () => screen.getByText(/tool call/).closest("details");

describe("a finished trace", () => {
  it("stays open when a gate refused a call, so the refusal is read", () => {
    const trace: Trace = [call("refused")];
    render(<RunTrace trace={trace} live={false} />);
    expect(details()).toHaveAttribute("open");
  });

  it("folds away when nothing was refused", () => {
    const trace: Trace = [call("ok")];
    render(<RunTrace trace={trace} live={false} />);
    expect(details()).not.toHaveAttribute("open");
  });
});
