import { describe, expect, it } from "vitest";
import { statusTone } from "./severity";

describe("statusTone", () => {
  it.each([
    ["success", "ok"],
    ["confirmation_required", "warn"],
    ["AWAITING_CONFIRMATION", "warn"],
    ["BLOCKED", "refused"],
    ["access_denied", "refused"],
    ["CALL_BUDGET_EXHAUSTED", "refused"],
    ["error", "crit"],
    ["something_else", "neutral"],
  ])("%s → %s", (status, tone) => {
    expect(statusTone(status)).toBe(tone);
  });
});
