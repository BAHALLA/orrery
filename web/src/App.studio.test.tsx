import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import { makeToken, jsonResponse, restoreGlobals } from "./test/appHarness";

// The Studio entry: offered to admins only.

afterEach(restoreGlobals);

describe("Studio entry", () => {
  function stubAs(role: string) {
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
          });
        }
        // A deployment without ORRERY_STUDIO_ENABLED: the routes are not mounted.
        if (u.startsWith("/studio")) return new Response("{}", { status: 404 });
        return jsonResponse({ sessions: [], entries: [], pending: null, severity: null });
      }),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "alice", roles: [role] }));
  }

  it("is offered to admins only, and loads on demand", async () => {
    stubAs("admin");
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /^studio/i }));
    expect(await screen.findByRole("heading", { name: "Studio" })).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "The Studio is not enabled here" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^studio/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("is not offered to an operator", async () => {
    stubAs("operator");
    render(<App />);
    expect(await screen.findByText("alice")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("operator")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^studio/i })).not.toBeInTheDocument();
  });
});
