import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import { makeToken, jsonResponse, restoreGlobals } from "./test/appHarness";

// The Studio entry: offered only where the server says this caller may use it.

afterEach(restoreGlobals);

describe("Studio entry", () => {
  function stubAs(role: string, studioAvailable = false) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        const u = String(url);
        if (u.endsWith("/me")) {
          return jsonResponse({
            subject: "alice",
            role,
            autonomy_level: null,
            model_provider: "gemini",
            model_name: "",
            self_test_available: true,
            studio_available: studioAvailable,
          });
        }
        if (u.startsWith("/studio")) return jsonResponse({ compositions: [], runs: [] });
        return jsonResponse({ sessions: [], entries: [], pending: null, severity: null });
      }),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "alice", roles: [role] }));
  }

  it("is offered where the server makes it available, and loads on demand", async () => {
    stubAs("admin", true);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /^studio/i }));
    expect(await screen.findByRole("heading", { name: "Studio" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^studio/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("is hidden from an admin when the deployment does not mount it", async () => {
    stubAs("admin", false);
    render(<App />);
    expect(await screen.findByText("alice")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("admin")).toBeInTheDocument());
    // The Agents entry shows, so the admin nav has rendered: the Studio is absent.
    expect(await screen.findByRole("button", { name: /^agents/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^studio/i })).not.toBeInTheDocument();
  });

  it("is not offered to an operator", async () => {
    stubAs("operator");
    render(<App />);
    expect(await screen.findByText("alice")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("operator")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^studio/i })).not.toBeInTheDocument();
  });
});
