import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import { jsonResponse, makeToken, restoreGlobals } from "./test/appHarness";

// The learning sandbox's hint ladder in the header (AEP-032 §3).

afterEach(restoreGlobals);

const ME = {
  subject: "alice",
  role: "viewer",
  autonomy_level: "L2",
  model_provider: "gemini",
  model_name: "",
  self_test_available: false,
};

function stub(learning: Record<string, unknown> | null) {
  let level = (learning?.level as number | undefined) ?? 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/me")) {
      return jsonResponse({ ...ME, learning: learning && { ...learning, level } });
    }
    if (url.endsWith("/learning/hint") && init?.method === "POST") {
      level = Math.min(level + 1, 3);
      return jsonResponse({ ...learning, level });
    }
    return jsonResponse({ sessions: [], entries: [], pending: null });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("learning sandbox", () => {
  it("shows the level and climbs one step per press, up to the reveal", async () => {
    const fetchMock = stub({
      scenario_id: "kafka-lag-01",
      level: 1,
      max_level: 3,
      can_take_hint: true,
    });
    localStorage.setItem(storageKeys.token, makeToken({ sub: "alice", roles: ["viewer"] }));
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByText("Learning sandbox")).toBeInTheDocument();
    expect(screen.getByText("Hint 1/3")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: /take a hint/i });
    expect(button).toHaveAccessibleDescription(/which tool to run.*recorded/i);

    await user.click(button);
    expect(await screen.findByText("Hint 2/3")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /take a hint/i }));
    expect(await screen.findByText("Hint 3/3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /take a hint/i })).toBeDisabled();

    const hints = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/learning/hint"));
    expect(hints).toHaveLength(2);
  });

  it("offers no button to a caller who may not take hints", async () => {
    stub({ scenario_id: "kafka-lag-01", level: 0, max_level: 3, can_take_hint: false });
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bot" }));
    render(<App />);
    expect(await screen.findByText("Hint 0/3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /take a hint/i })).not.toBeInTheDocument();
  });

  it("shows nothing outside the sandbox", async () => {
    stub(null);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "alice" }));
    render(<App />);
    expect(await screen.findByRole("textbox", { name: /message/i })).toBeInTheDocument();
    expect(screen.queryByText("Learning sandbox")).not.toBeInTheDocument();
  });
});
