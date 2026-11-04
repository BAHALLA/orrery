import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Role } from "../api/types";
import { Welcome } from "./Welcome";

function copy(me: { role: Role; autonomy_level: string | null } | null): string {
  render(<Welcome onSuggest={vi.fn()} onSweep={vi.fn()} specialists={[]} me={me} />);
  return screen.getByText(/^Ask about your systems\./).textContent ?? "";
}

const PROMISE = /waits for your approval/;

describe("Welcome", () => {
  it("has a plain headline", () => {
    copy(null);
    expect(screen.getByRole("heading")).toHaveTextContent("What would you like to check?");
  });

  it("makes no claim until /me has answered", () => {
    const text = copy(null);
    expect(text).not.toMatch(PROMISE);
    expect(text).not.toMatch(/read-only|viewer|can't|won't|change/);
  });

  it("makes no claim for a role or level it doesn't know", () => {
    const unknownRole = copy({ role: "auditor" as Role, autonomy_level: "L3" });
    expect(unknownRole).not.toMatch(PROMISE);
    expect(unknownRole).not.toMatch(/viewer/);
  });

  it("makes no promise for a level it doesn't know", () => {
    expect(copy({ role: "operator", autonomy_level: "L9" })).not.toMatch(PROMISE);
  });

  it.each([
    [null, /viewer role can't change systems/],
    ["L3", /viewer role can't change systems/],
    ["L4", /viewer role can't change systems/],
    ["L2", /Neither your viewer role nor this read-only deployment \(autonomy L2\)/],
  ])("tells a viewer at autonomy %s they can investigate, not change", (level, expected) => {
    const text = copy({ role: "viewer", autonomy_level: level });
    expect(text).toMatch(/You can investigate/);
    expect(text).toMatch(expected);
    expect(text).not.toMatch(PROMISE);
  });

  it.each(["operator", "admin"] as const)(
    "tells an %s at L2 the deployment is read-only",
    (role) => {
      const text = copy({ role, autonomy_level: "L2" });
      expect(text).toMatch(/This deployment is read-only \(autonomy L2\)/);
      expect(text).not.toMatch(PROMISE);
    },
  );

  it.each([
    ["operator", null, /approval first\.$/],
    ["admin", null, /approval first\.$/],
    ["operator", "L3", /destructive actions are off here/],
    ["admin", "L3", /destructive actions are off here/],
    ["operator", "L4", /destructive actions need an admin/],
    ["admin", "L4", /destructive actions included/],
  ] as const)("keeps the approval promise for %s at %s", (role, level, expected) => {
    const text = copy({ role, autonomy_level: level });
    expect(text).toMatch(PROMISE);
    expect(text).toMatch(expected);
  });
});
