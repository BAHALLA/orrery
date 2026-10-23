import { describe, expect, it } from "vitest";
import { decisionFor, decisionOf, decisionTarget } from "./decision";

describe("decisions", () => {
  it("names the action by its most identifying argument", () => {
    expect(decisionTarget({ namespace: "prod", name: "orders-worker" })).toBe("orders-worker");
    expect(decisionTarget({ replicas: 3, topic: "orders" })).toBe("orders");
    expect(decisionTarget({ flavour: "x" })).toBe("x");
    expect(decisionTarget({ replicas: 3 })).toBeNull();
  });

  it("snapshots the pending action at click time", () => {
    const pending = {
      tool_name: "restart_container",
      level: "confirm",
      args: { container: "orders-worker" },
      created_at: 1,
    };
    expect(decisionFor("approve", pending)).toEqual({
      word: "approve",
      tool: "restart_container",
      target: "orders-worker",
    });
    expect(decisionFor("deny", null)).toEqual({ word: "deny", tool: null, target: null });
  });

  it("recognises the button words in a reloaded transcript, and nothing else", () => {
    const user = (text: string) => ({ id: "1", role: "user" as const, text, at: 0 });
    expect(decisionOf(user("approve"))).toEqual({ word: "approve", tool: null, target: null });
    expect(decisionOf(user(" Deny "))).toMatchObject({ word: "deny" });
    expect(decisionOf(user("approve the restart please"))).toBeNull();
    expect(decisionOf({ ...user("approve"), role: "assistant" })).toBeNull();
  });
});
