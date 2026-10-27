import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { storageKeys } from "./config";
import { makeToken, jsonResponse, restoreGlobals } from "./test/appHarness";

// Conversation history: the sidebar, the server store and one session per thread.

afterEach(restoreGlobals);

describe("App auth flow", () => {
  it("restores history from the server in a browser that has never seen it", async () => {
    // The point of the server-backed history: a different machine (or cleared
    // site data) rebuilds the sidebar and the transcript from the session store.
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (path === "/sessions") {
        return jsonResponse({
          sessions: [
            { session_id: "s-old", title: "check kafka", last_update_time: 100 },
            { session_id: "s-new", title: "restart payment-api", last_update_time: 200 },
          ],
        });
      }
      if (path === "/session/s-new") {
        return jsonResponse({
          session_id: "s-new",
          title: "restart payment-api",
          messages: [
            { role: "user", text: "restart payment-api", at: 200 },
            { role: "assistant", text: "Rolled the deployment.", at: 201 },
          ],
          last_update_time: 201,
        });
      }
      return jsonResponse({ entries: [], pending: null, severity: null, report: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    render(<App />);

    // Both conversations are listed, newest first, with the latest one open.
    const history = await screen.findByRole("navigation", { name: /conversation history/i });
    await waitFor(() =>
      expect(within(history).getByText("restart payment-api")).toBeInTheDocument(),
    );
    expect(within(history).getByText("check kafka")).toBeInTheDocument();
    expect(await screen.findByText("Rolled the deployment.")).toBeInTheDocument();
  });

  it("opens an older conversation and fetches its transcript on demand", async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (path === "/sessions") {
        return jsonResponse({
          sessions: [
            { session_id: "s-old", title: "check kafka", last_update_time: 100 },
            { session_id: "s-new", title: "restart payment-api", last_update_time: 200 },
          ],
        });
      }
      if (path === "/session/s-old") {
        return jsonResponse({
          session_id: "s-old",
          title: "check kafka",
          messages: [{ role: "assistant", text: "All brokers up.", at: 100 }],
          last_update_time: 100,
        });
      }
      if (path === "/session/s-new") {
        return jsonResponse({
          session_id: "s-new",
          title: "restart payment-api",
          messages: [],
          last_update_time: 200,
        });
      }
      return jsonResponse({ entries: [], pending: null, severity: null, report: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    const history = await screen.findByRole("navigation", { name: /conversation history/i });
    await waitFor(() => expect(within(history).getByText("check kafka")).toBeInTheDocument());
    // The older conversation's transcript is only fetched when it is opened.
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith("/session/s-old"))).toBe(false);

    await user.click(within(history).getByText("check kafka"));

    expect(await screen.findByText("All brokers up.")).toBeInTheDocument();
  });

  it("deletes a conversation from the store, not just the sidebar", async () => {
    const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      if (path === "/sessions") {
        return jsonResponse({
          sessions: [
            { session_id: "s1", title: "check kafka", last_update_time: 100 },
            { session_id: "s2", title: "restart payment-api", last_update_time: 200 },
          ],
        });
      }
      if (path.startsWith("/session/")) {
        return jsonResponse({
          session_id: path.slice("/session/".length),
          title: "",
          messages: [],
          last_update_time: 0,
        });
      }
      return jsonResponse({ entries: [], pending: null, severity: null, report: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    const history = await screen.findByRole("navigation", { name: /conversation history/i });
    await waitFor(() => expect(within(history).getByText("check kafka")).toBeInTheDocument());

    const isDeleted = () =>
      fetchMock.mock.calls.some(
        ([u, init]) => String(u).endsWith("/session/s1") && init?.method === "DELETE",
      );

    // The row goes at once, behind an Undo; nothing is deleted yet.
    await user.click(screen.getByRole("button", { name: /delete conversation: check kafka/i }));
    expect(within(history).queryByText("check kafka")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(within(history).getByText("check kafka")).toBeInTheDocument();
    expect(isDeleted()).toBe(false);

    // Dismissing the notice deletes without waiting out the window.
    await user.click(screen.getByRole("button", { name: /delete conversation: check kafka/i }));
    await user.click(screen.getByRole("button", { name: /dismiss and delete now/i }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, init]) => String(u).endsWith("/session/s1") && init?.method === "DELETE",
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(within(history).queryByText("check kafka")).not.toBeInTheDocument());
  });

  it("keeps conversations in a sidebar history", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ session_id: "s1", response: "ok" })),
    );
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /message/i }), "check kafka");
    await user.click(screen.getByRole("button", { name: /send/i }));
    await screen.findByText("ok");

    // The conversation is titled from the first message and listed under History.
    const history = screen.getByRole("navigation", { name: /conversation history/i });
    expect(within(history).getByText("check kafka")).toBeInTheDocument();

    // New chat adds a fresh entry and clears the transcript.
    await user.click(screen.getByRole("button", { name: /new chat/i }));
    expect(screen.queryByText("ok")).not.toBeInTheDocument();
  });

  it("keeps one session per thread: New chat starts another, going back continues the first", async () => {
    // The server mints a session per id-less turn and echoes a known id back.
    let minted = 0;
    const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).endsWith("/chat/stream")) {
        const body = JSON.parse(String(init?.body));
        const session_id = body.session_id || `s${++minted}`;
        return jsonResponse({ session_id, response: `reply in ${session_id}` });
      }
      return jsonResponse({ entries: [], pending: null, severity: null, report: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem(storageKeys.token, makeToken({ sub: "bob", roles: ["admin"] }));
    const sent = () =>
      fetchMock.mock.calls
        .filter(([u]) => String(u).endsWith("/chat/stream"))
        .map(([, init]) => JSON.parse(String(init?.body)).session_id ?? null);

    const user = userEvent.setup();
    render(<App />);
    const say = async (text: string) => {
      await user.type(screen.getByRole("textbox", { name: /message/i }), text);
      await user.click(screen.getByRole("button", { name: /send/i }));
    };

    await say("check kafka");
    await screen.findByText("reply in s1");
    await say("and the lag?");
    await waitFor(() => expect(sent()).toEqual([null, "s1"]));

    await user.click(screen.getByRole("button", { name: /new chat/i }));
    await say("restart payment-api");
    await screen.findByText("reply in s2");
    await waitFor(() => expect(sent()).toEqual([null, "s1", null]));

    const history = screen.getByRole("navigation", { name: /conversation history/i });
    await user.click(within(history).getByText("check kafka"));
    await say("still lagging?");
    await waitFor(() => expect(sent()).toEqual([null, "s1", null, "s1"]));
  });
});
