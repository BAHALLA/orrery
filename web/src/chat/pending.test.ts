import { describe, expect, it } from "vitest";
import type { PendingConfirmation } from "../api/types";
import { pendingPlacement } from "./pending";

const base: PendingConfirmation = {
  tool_name: "restart_container",
  level: "confirm",
  args: { name: "orders-worker" },
  created_at: 1,
};

describe("pendingPlacement", () => {
  it("shows nothing when nothing is pending", () => {
    expect(pendingPlacement(null, "s1")).toBe("none");
  });

  it("shows the card only in the conversation the action is waiting in", () => {
    const pending = { ...base, session_id: "s1" };
    expect(pendingPlacement(pending, "s1")).toBe("here");
    expect(pendingPlacement(pending, "s2")).toBe("elsewhere");
    // A draft that has no session yet is not where it is waiting either.
    expect(pendingPlacement(pending, null)).toBe("elsewhere");
  });

  it("keeps the card everywhere for a server that doesn't say where", () => {
    expect(pendingPlacement(base, "s2")).toBe("here");
    expect(pendingPlacement({ ...base, session_id: null }, "s3")).toBe("here");
    // …but not in an unsent draft, which never showed it.
    expect(pendingPlacement(base, null)).toBe("none");
  });
});
