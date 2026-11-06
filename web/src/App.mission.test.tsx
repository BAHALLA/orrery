import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import { jsonResponse, makeToken, restoreGlobals } from "./test/appHarness";

// A lab's brief: on the empty chat, and kept to hand once the chat has moved on.

afterEach(() => {
  vi.useRealTimers();
  restoreGlobals();
});

const MISSION = {
  title: "Consumer lag on orders",
  situation: "An alert just fired.",
  goal: "Find the cause.",
  tasks_url: "https://example.com/labs/lag",
  tasks_label: "Open the tasks",
  starters: [],
};

function stub(mission: typeof MISSION | null, meStatus: () => number = () => 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (!String(input).endsWith("/me")) {
        return jsonResponse({ sessions: [], entries: [], pending: null });
      }
      const status = meStatus();
      return status === 200
        ? jsonResponse({
            subject: "alice",
            role: "viewer",
            autonomy_level: "L2",
            model_provider: "gemini",
            model_name: "",
            self_test_available: false,
            mission,
          })
        : new Response("{}", { status });
    }),
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

  it("shows no stock suggestions while /me is failing, and retries", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = 502;
    stub(MISSION, () => status);
    render(<App />);
    await screen.findByText("What would you like to check?");
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(screen.queryByRole("button", { name: /Kafka consumer lag/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Full health sweep/ })).not.toBeInTheDocument();

    status = 200;
    await act(() => vi.advanceTimersByTimeAsync(2100));
    expect(await screen.findByRole("heading", { name: MISSION.title })).toBeInTheDocument();
  });

  it("still shows the stock suggestions on a server without /me", async () => {
    stub(null, () => 404);
    render(<App />);
    expect(await screen.findByRole("button", { name: /Full health sweep/ })).toBeInTheDocument();
  });

  it("gives no retry to a refused token", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const asked = vi.fn(() => 403);
    stub(null, asked);
    render(<App />);
    await screen.findByText("What would you like to check?");
    await act(() => vi.advanceTimersByTimeAsync(20000));
    expect(asked).toHaveBeenCalledTimes(1);
  });
});
