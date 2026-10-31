import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { makeToken, restoreGlobals } from "./test/appHarness";

// Signing in: the token gate, and the console it opens onto.

afterEach(restoreGlobals);

describe("sign-in", () => {
  it("shows the token gate when unauthenticated", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: /orrery console/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/jwt bearer token/i)).toBeInTheDocument();
  });

  it("enters the console and shows the identity badge after connecting", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.type(
      screen.getByLabelText(/jwt bearer token/i),
      makeToken({ sub: "alice", roles: ["operator"] }),
    );
    await user.click(screen.getByRole("button", { name: /connect/i }));

    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("operator")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /message/i })).toBeInTheDocument();
  });
});

describe("access denied", () => {
  beforeEach(() => {
    window.__ORRERY_CONSOLE_CONFIG__ = { accessRestricted: true };
    vi.resetModules();
  });
  afterEach(() => {
    delete window.__ORRERY_CONSOLE_CONFIG__;
  });

  async function connect() {
    const user = userEvent.setup();
    const { App: Fresh } = await import("./App");
    render(<Fresh />);
    await user.type(screen.getByLabelText(/jwt bearer token/i), makeToken({ sub: "bob" }));
    await user.click(screen.getByRole("button", { name: /connect/i }));
  }

  it("shows the no-access page and loads nothing else", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL) =>
        new Response(JSON.stringify({ detail: "access_denied" }), {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await connect();

    expect(
      await screen.findByRole("heading", { name: /don.t have access to this deployment/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /message/i })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toEqual(["/me"]);

    await userEvent.setup().click(screen.getByRole("button", { name: /sign out/i }));
    expect(await screen.findByLabelText(/jwt bearer token/i)).toBeInTheDocument();
  });

  it("opens the console on any other failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 500 })),
    );
    await connect();
    expect(await screen.findByRole("textbox", { name: /message/i })).toBeInTheDocument();
  });
});

describe("access check across token renewals", () => {
  it("asks once per person, so a silent renew keeps the console mounted", async () => {
    const { renderHook, waitFor } = await import("@testing-library/react");
    const { useAccess } = await import("./auth/useAccess");
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL) =>
        new Response(JSON.stringify({ subject: "alice" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender } = renderHook(
      ({ who, token }: { who: string; token: string }) => useAccess(who, token, true),
      { initialProps: { who: "alice", token: "t1" } },
    );
    await waitFor(() => expect(result.current).toBe("granted"));

    rerender({ who: "alice", token: "t2" }); // SSO silent renew
    expect(result.current).toBe("granted");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Signing out and back in as the same person, now refused: the old
    // verdict must not show while the new /me is in flight.
    fetchMock.mockImplementationOnce(
      async () =>
        new Response(JSON.stringify({ detail: "access_denied" }), {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }),
    );
    rerender({ who: null as unknown as string, token: "" });
    rerender({ who: "alice", token: "t2b" });
    expect(result.current).toBe("checking");
    await waitFor(() => expect(result.current).toBe("denied"));

    rerender({ who: "bob", token: "t3" }); // someone else: checked afresh
    expect(result.current).toBe("checking");
    await waitFor(() => expect(result.current).toBe("granted"));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
