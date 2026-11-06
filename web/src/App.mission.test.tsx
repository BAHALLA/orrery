import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import { jsonResponse, makeToken, restoreGlobals } from "./test/appHarness";

// A lab's brief: on the empty chat, and kept to hand once the chat has moved on.

afterEach(restoreGlobals);

const MISSION = {
  title: "Consumer lag on orders",
  situation: "An alert just fired.",
  goal: "Find the cause.",
  tasks_url: "https://example.com/labs/lag",
  tasks_label: "Open the tasks",
  starters: [],
};

function stub(mission: typeof MISSION | null) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith("/me")
        ? jsonResponse({
            subject: "alice",
            role: "viewer",
            autonomy_level: "L2",
            model_provider: "gemini",
            model_name: "",
            self_test_available: false,
            mission,
          })
        : jsonResponse({ sessions: [], entries: [], pending: null }),
    ),
  );
  localStorage.setItem(storageKeys.token, makeToken({ sub: "alice", roles: ["viewer"] }));
}

describe("a lab's mission", () => {
  it("opens from the header, in the inspector", async () => {
    stub(MISSION);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Mission" }));

    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByRole("heading", { name: MISSION.title })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Mission" })).toHaveAttribute("aria-selected", "true");
  });

  it("offers nothing where the deployment has none", async () => {
    stub(null);
    render(<App />);
    expect(await screen.findByText("What would you like to check?")).toBeInTheDocument();
    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mission" })).not.toBeInTheDocument();
  });
});
