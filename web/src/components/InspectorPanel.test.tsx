import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { SystemController } from "../system/useSystem";
import { InspectorPanel } from "./InspectorPanel";

describe("the Mission tab when the brief has gone", () => {
  const system = { selfTest: null } as unknown as SystemController;

  it("falls back to a real tab, so the tablist stays usable", () => {
    const onTab = vi.fn();
    render(
      <InspectorPanel
        tab="mission"
        onTab={onTab}
        onClose={vi.fn()}
        activity={[]}
        triage={null}
        system={system}
        mission={null}
      />,
    );
    const selected = screen.getByRole("tab", { selected: true });
    expect(selected).toHaveTextContent("Tool calls");
    expect(selected).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Tool calls");

    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowLeft" });
    expect(onTab).toHaveBeenCalledWith("system");
  });
});
