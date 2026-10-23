import { describe, expect, it } from "vitest";
import type { ProgressEvent } from "../api/types";
import { agentLabel, applyProgress, flatten, refusalReason, summarize, type Trace } from "./trace";

function step(over: Partial<ProgressEvent>): ProgressEvent {
  return {
    call_id: "c",
    phase: "start",
    agent: "orrery_chat_agent",
    tool: "t",
    delegation: false,
    guarded: false,
    outcome: null,
    status: "",
    elapsed_ms: null,
    ...over,
  };
}

function run(steps: ProgressEvent[]): Trace {
  return steps.reduce<Trace>(applyProgress, []);
}

describe("applyProgress", () => {
  it("nests a specialist's calls under the delegation that asked it", () => {
    const trace = run([
      step({ call_id: "d1", tool: "kafka_health_agent", delegation: true }),
      step({ call_id: "t1", agent: "kafka_health_agent", tool: "get_consumer_lag" }),
      step({
        call_id: "t1",
        phase: "end",
        agent: "kafka_health_agent",
        tool: "get_consumer_lag",
        outcome: "ok",
        elapsed_ms: 40,
      }),
      step({
        call_id: "d1",
        phase: "end",
        tool: "kafka_health_agent",
        delegation: true,
        outcome: "ok",
      }),
    ]);

    expect(trace).toHaveLength(1);
    expect(trace[0].state).toBe("ok");
    expect(trace[0].children.map((c) => [c.tool, c.state, c.elapsedMs])).toEqual([
      ["get_consumer_lag", "ok", 40],
    ]);
  });

  it("keeps parallel delegations apart", () => {
    const trace = run([
      step({ call_id: "d1", tool: "kafka_health_agent", delegation: true }),
      step({ call_id: "d2", tool: "k8s_health_agent", delegation: true }),
      step({ call_id: "t1", agent: "k8s_health_agent", tool: "list_pods" }),
      step({ call_id: "t2", agent: "kafka_health_agent", tool: "list_topics" }),
    ]);

    expect(trace.map((d) => [d.tool, d.children.map((c) => c.tool)])).toEqual([
      ["kafka_health_agent", ["list_topics"]],
      ["k8s_health_agent", ["list_pods"]],
    ]);
  });

  it("records a call paused for approval as awaiting, not refused", () => {
    for (const status of ["confirmation_required", "AWAITING_CONFIRMATION"]) {
      const trace = run([
        step({ call_id: "t1", tool: "restart_deployment", guarded: true }),
        step({
          call_id: "t1",
          phase: "end",
          tool: "restart_deployment",
          guarded: true,
          outcome: "refused",
          status,
        }),
      ]);

      expect(trace[0]).toMatchObject({ state: "awaiting", status, guarded: true });
    }
  });

  it.each([
    "BLOCKED",
    "access_denied",
    "CALL_BUDGET_EXHAUSTED",
    "DELEGATION_BUDGET_EXHAUSTED",
    "REPEATED_FAILURE",
    "error",
  ])("records a gate's %s as refused, not as a failure", (status) => {
    const trace = run([
      step({
        call_id: "t1",
        phase: "end",
        tool: "scale_deployment",
        guarded: true,
        outcome: "refused",
        status,
      }),
    ]);
    expect(trace[0].state).toBe("refused");
  });

  it("keeps a tool's own error status a failure", () => {
    const trace = run([step({ call_id: "t1", phase: "end", outcome: "error", status: "error" })]);
    expect(trace[0].state).toBe("error");
  });

  it("accepts an end whose start it never saw", () => {
    const trace = run([step({ call_id: "late", phase: "end", tool: "get_nodes", outcome: "ok" })]);

    expect(trace).toHaveLength(1);
    expect(trace[0].state).toBe("ok");
  });
});

describe("labels and summary", () => {
  it("names the specialists an operator knows", () => {
    expect(agentLabel("kafka_health_agent")).toBe("Kafka");
    expect(agentLabel("k8s_health_agent")).toBe("Kubernetes");
    expect(agentLabel("custom_billing_agent")).toBe("Custom billing");
  });

  it("summarizes who was consulted and how much ran", () => {
    const trace = run([
      step({ call_id: "d1", tool: "kafka_health_agent", delegation: true }),
      step({ call_id: "t1", agent: "kafka_health_agent", tool: "a" }),
      step({ call_id: "t2", agent: "kafka_health_agent", tool: "b" }),
    ]);

    expect(flatten(trace)).toHaveLength(3);
    expect(summarize(trace)).toBe("Kafka · 2 tool calls");
  });

  it("counts awaiting and refused calls separately", () => {
    const end = (call_id: string, status: string) =>
      step({ call_id, phase: "end", outcome: "refused", status });
    const trace = run([
      end("a", "confirmation_required"),
      end("b", "BLOCKED"),
      end("c", "access_denied"),
    ]);
    expect(summarize(trace)).toBe("3 tool calls · 1 awaiting approval · 2 refused");
  });

  it("explains a refusal as policy, not as a fault", () => {
    expect(refusalReason("BLOCKED")).toMatch(/autonomy level/);
    expect(refusalReason("access_denied")).toMatch(/role/);
    expect(refusalReason("CALL_BUDGET_EXHAUSTED")).toMatch(/budget/);
    expect(refusalReason("error")).toMatch(/circuit open/);
    expect(refusalReason("")).toBe("refused by a guard");
  });
});
