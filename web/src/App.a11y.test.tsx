import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import { makeToken, jsonResponse, restoreGlobals } from "./test/appHarness";
import { stubWideScreen } from "./test/reactflow";

// Accessibility of the transcript and the inspector drawer.

afterEach(restoreGlobals);

describe("accessibility", () => {
  function stubQuiet() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        const u = String(url);
        if (u.endsWith("/chat/stream") || u.endsWith("/chat")) {
          return jsonResponse({ session_id: "s1", response: "All brokers up." });
        }
        return jsonResponse({ sessions: [], entries: [], pending: null, severity: null });
      }),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));
  }

  it("announces the turn once instead of reading every token", async () => {
    stubQuiet();
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: /message/i }), "brokers?");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText("All brokers up.")).toBeInTheDocument();
    const transcript = screen.getByRole("log", { name: /conversation transcript/i });
    expect(transcript).not.toHaveAttribute("aria-live");
    expect(transcript).toHaveAttribute("aria-busy", "false");
    await waitFor(() =>
      expect(screen.getAllByRole("status").some((s) => s.textContent === "Answer ready")).toBe(
        true,
      ),
    );
  });

  it("inspector tabs follow the tabs pattern, and the drawer keeps and returns focus", async () => {
    stubQuiet();
    const user = userEvent.setup();
    render(<App />);

    const opener = await screen.findByRole("button", { name: /tool calls/i });
    await user.click(opener);

    // jsdom has no matchMedia, so this is the narrow layout: a modal drawer.
    const drawer = await screen.findByRole("dialog", { name: /inspector/i });
    expect(drawer).toContainElement(document.activeElement as HTMLElement);

    const tools = within(drawer).getByRole("tab", { name: /tool calls/i });
    expect(tools).toHaveAttribute("aria-selected", "true");
    expect(tools).toHaveAttribute("tabindex", "0");
    const panel = within(drawer).getByRole("tabpanel");
    expect(tools).toHaveAttribute("aria-controls", panel.id);
    expect(panel).toHaveAttribute("aria-labelledby", tools.id);

    tools.focus();
    await user.keyboard("{ArrowRight}");
    const triage = within(drawer).getByRole("tab", { name: /triage/i });
    expect(triage).toHaveAttribute("aria-selected", "true");
    expect(triage).toHaveFocus();
    expect(tools).toHaveAttribute("tabindex", "-1");
    await user.keyboard("{End}");
    expect(within(drawer).getByRole("tab", { name: /system/i })).toHaveFocus();

    // Tab wraps inside the drawer rather than escaping behind the scrim.
    const close = within(drawer).getByRole("button", { name: /close inspector/i });
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(drawer).toContainElement(document.activeElement as HTMLElement);
    }
    close.focus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: /inspector/i })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("folds the navigation to its icons on a wide screen, and remembers it", async () => {
    stubWideScreen();
    stubQuiet();
    const user = userEvent.setup();
    const { unmount } = render(<App />);

    const nav = await screen.findByRole("complementary", { name: "Navigation" });
    expect(within(nav).getByRole("navigation", { name: /history/i })).toBeVisible();
    await user.click(within(nav).getByRole("button", { name: "Collapse sidebar" }));
    // Labels stay for screen readers; the history list is folded away.
    expect(within(nav).getByRole("button", { name: /new chat/i })).toBeInTheDocument();
    expect(within(nav).queryByRole("navigation", { name: /history/i })).not.toBeInTheDocument();
    expect(localStorage.getItem(storageKeys.navCollapsed)).toBe("1");

    unmount();
    render(<App />);
    const again = await screen.findByRole("complementary", { name: "Navigation" });
    expect(within(again).getByRole("button", { name: "Expand sidebar" })).toBeInTheDocument();

    // Ctrl+B unfolds it.
    await user.keyboard("{Control>}b{/Control}");
    expect(within(again).getByRole("button", { name: "Collapse sidebar" })).toBeInTheDocument();
  });
});
