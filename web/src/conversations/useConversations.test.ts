import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../chat/types";
import { legacyStorageKeys } from "../config";
import { NEW_CONVERSATION_TITLE } from "./types";
import { useConversations } from "./useConversations";

function msg(id: string): ChatMessage {
  return { id, role: "assistant", text: "reply", at: 1 };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function summary(id: string, title: string, updated: number) {
  return { session_id: id, title, last_update_time: updated };
}

interface Handlers {
  sessions?: () => Response | Promise<Response>;
  session?: (id: string) => Response | Promise<Response>;
  del?: (id: string) => Response | Promise<Response>;
}

/** Route each endpoint to a canned response, defaulting to "no history". */
function stubApi(handlers: Handlers = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), "http://localhost").pathname;
    const id = decodeURIComponent(path.replace("/session/", ""));
    if (init?.method === "DELETE") {
      return handlers.del?.(id) ?? new Response(null, { status: 204 });
    }
    if (path === "/sessions") return handlers.sessions?.() ?? json({ sessions: [] });
    if (path.startsWith("/session/")) {
      return (
        handlers.session?.(id) ??
        json({ session_id: id, title: "", messages: [], last_update_time: 0 })
      );
    }
    return json({});
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Render the hook and wait for the initial history fetch to settle. */
async function renderLoaded(handlers: Handlers = {}) {
  const fetchMock = stubApi(handlers);
  const rendered = renderHook(() => useConversations("tok"));
  await waitFor(() => expect(rendered.result.current.isLoading).toBe(false));
  return { ...rendered, fetchMock };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("useConversations — server-backed history", () => {
  it("starts with one empty conversation when the store has none", async () => {
    const { result } = await renderLoaded();
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.active.title).toBe(NEW_CONVERSATION_TITLE);
    expect(result.current.active.sessionId).toBeNull();
  });

  it("rebuilds the sidebar from the store, newest first", async () => {
    const { result } = await renderLoaded({
      sessions: () =>
        json({
          sessions: [
            summary("old", "check kafka", 100),
            summary("new", "restart payment-api", 200),
          ],
        }),
    });

    expect(result.current.conversations.map((c) => c.title)).toEqual([
      "restart payment-api",
      "check kafka",
    ]);
    // Coming back to the console opens the conversation you were last in, and
    // the untouched draft is dropped rather than sitting above real history.
    expect(result.current.activeId).toBe("new");
    expect(result.current.conversations.map((c) => c.sessionId)).toEqual(["new", "old"]);
  });

  it("labels an untitled stored session with the placeholder", async () => {
    const { result } = await renderLoaded({
      sessions: () => json({ sessions: [summary("s1", "", 5)] }),
    });
    expect(result.current.active.title).toBe(NEW_CONVERSATION_TITLE);
  });

  it("fetches the transcript of the conversation it opens", async () => {
    const { result } = await renderLoaded({
      sessions: () => json({ sessions: [summary("s1", "check kafka", 100)] }),
      session: (id) =>
        json({
          session_id: id,
          title: "check kafka",
          messages: [
            { role: "user", text: "check kafka", at: 100 },
            { role: "assistant", text: "All brokers up.", at: 101 },
          ],
          last_update_time: 101,
        }),
    });

    await waitFor(() => expect(result.current.active.loaded).toBe(true));
    expect(result.current.active.messages.map((m) => m.text)).toEqual([
      "check kafka",
      "All brokers up.",
    ]);
    // Server seconds become browser milliseconds.
    expect(result.current.active.messages[0].at).toBe(100_000);
  });

  it("loads a transcript once, however often it is reselected", async () => {
    const { result, fetchMock } = await renderLoaded({
      sessions: () => json({ sessions: [summary("a", "A", 2), summary("b", "B", 1)] }),
    });
    await waitFor(() => expect(result.current.active.loaded).toBe(true));

    act(() => result.current.selectConversation("b"));
    await waitFor(() => expect(result.current.active.loaded).toBe(true));
    act(() => result.current.selectConversation("a"));
    act(() => result.current.selectConversation("b"));

    await waitFor(() => {
      const transcripts = fetchMock.mock.calls.filter(([u]) => /\/session\/[ab]$/.test(String(u)));
      expect(transcripts).toHaveLength(2); // one per conversation, not one per click
    });
  });

  it("keeps a turn sent while the history list was still in flight", async () => {
    // The reply landed in the draft before /sessions answered; hydrating must
    // not re-add the server's row for that same session as a second entry.
    let release: (r: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => (release = resolve));
    stubApi({ sessions: () => pending });
    const { result } = renderHook(() => useConversations("tok"));

    act(() => result.current.patchActive({ sessionId: "s1", messages: [msg("m1")] }));
    act(() => release(json({ sessions: [summary("s1", "check kafka", 5)] })));

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.active.messages.map((m) => m.id)).toEqual(["m1"]);
  });

  it("stays usable when the history list fails to load", async () => {
    const { result } = await renderLoaded({ sessions: () => json({ detail: "boom" }, 500) });
    expect(result.current.error).toMatch(/server failed/i);
    // A console with no history is still a console: the draft is there to type in.
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.active.sessionId).toBeNull();
  });

  it("reports a transcript that will not load, leaving it retryable", async () => {
    const { result } = await renderLoaded({
      sessions: () => json({ sessions: [summary("s1", "check kafka", 5)] }),
      session: () => json({ detail: "nope" }, 500),
    });
    await waitFor(() => expect(result.current.error).toMatch(/server failed/i));
    expect(result.current.active.loaded).toBe(false);
  });

  it("newConversation reuses an existing blank instead of piling up", async () => {
    const { result } = await renderLoaded();
    const firstId = result.current.activeId;
    act(() => result.current.newConversation());
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.activeId).toBe(firstId);
  });

  it("newConversation adds a fresh one once the current has messages", async () => {
    const { result } = await renderLoaded();
    act(() => result.current.patchActive({ messages: [msg("m")] }));
    act(() => result.current.newConversation());
    expect(result.current.conversations).toHaveLength(2);
    expect(result.current.active.messages).toHaveLength(0);
  });

  it("keeps a reply that arrives in the same tick as a New-chat click", async () => {
    // The regression: newConversation wrote a whole new array built from the
    // render closure, so an update queued earlier in the same tick — the
    // assistant's reply landing via patchActive — was silently overwritten.
    const { result } = await renderLoaded();
    act(() => result.current.patchActive({ messages: [msg("user-1")] }));
    const originalId = result.current.activeId;

    act(() => {
      result.current.patchActive((c) => ({ messages: [...c.messages, msg("assistant-reply")] }));
      result.current.newConversation();
    });

    const original = result.current.conversations.find((c) => c.id === originalId);
    expect(original?.messages.map((m) => m.id)).toEqual(["user-1", "assistant-reply"]);
    expect(result.current.conversations).toHaveLength(2);
  });
});

describe("useConversations — deletion", () => {
  it("deletes the stored session, then drops the row", async () => {
    const { result, fetchMock } = await renderLoaded({
      sessions: () => json({ sessions: [summary("a", "A", 2), summary("b", "B", 1)] }),
    });

    await act(() => result.current.deleteConversation("a"));

    expect(
      fetchMock.mock.calls.some(
        ([u, init]) => String(u).endsWith("/session/a") && init?.method === "DELETE",
      ),
    ).toBe(true);
    expect(result.current.conversations.map((c) => c.id)).toEqual(["b"]);
    expect(result.current.activeId).toBe("b");
  });

  it("deleting a draft touches no endpoint", async () => {
    const { result, fetchMock } = await renderLoaded();
    const draftId = result.current.activeId;
    fetchMock.mockClear();

    await act(() => result.current.deleteConversation(draftId));

    expect(fetchMock).not.toHaveBeenCalled();
    // The last conversation is replaced by a fresh empty one, never nothing.
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.active.messages).toHaveLength(0);
  });

  it("keeps the row when the server refuses the delete", async () => {
    const { result } = await renderLoaded({
      sessions: () => json({ sessions: [summary("a", "A", 2)] }),
      del: () => json({ detail: "nope" }, 500),
    });

    await act(() => result.current.deleteConversation("a"));

    // The list still says what the store says — the failure is reported instead.
    expect(result.current.conversations.map((c) => c.id)).toEqual(["a"]);
    expect(result.current.error).toMatch(/server failed/i);
  });

  it("drops the row when the session is already gone (404)", async () => {
    const { result } = await renderLoaded({
      sessions: () => json({ sessions: [summary("a", "A", 2), summary("b", "B", 1)] }),
      del: () => json({ detail: "Session not found" }, 404),
    });

    await act(() => result.current.deleteConversation("a"));

    expect(result.current.conversations.map((c) => c.id)).toEqual(["b"]);
    expect(result.current.error).toBeNull();
  });
});

describe("useConversations — no local transcripts", () => {
  it("writes nothing to localStorage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { result } = await renderLoaded({
      sessions: () => json({ sessions: [summary("s1", "check kafka", 5)] }),
    });
    act(() => result.current.patchActive({ messages: [msg("m")] }));
    expect(setItem).not.toHaveBeenCalled();
  });

  it("purges history an earlier build left on disk", async () => {
    legacyStorageKeys.forEach((key) => localStorage.setItem(key, "stale"));
    await renderLoaded();
    legacyStorageKeys.forEach((key) => expect(localStorage.getItem(key)).toBeNull());
  });
});

describe("useConversations — streaming writes", () => {
  it("a write with touch: false keeps the row's time and the history's identity", async () => {
    const { result } = await renderLoaded({
      sessions: () => json({ sessions: [summary("a", "A", 2), summary("b", "B", 1)] }),
    });
    const before = result.current.history;
    const updatedAt = result.current.conversations.find((c) => c.id === "b")?.updatedAt;

    // A streamed delta into the older conversation: not new activity.
    act(() =>
      result.current.patchConversation("b", { messages: [msg("partial")] }, { touch: false }),
    );

    const b = result.current.conversations.find((c) => c.id === "b");
    expect(b?.messages.map((m) => m.id)).toEqual(["partial"]);
    expect(b?.updatedAt).toBe(updatedAt);
    // Same order, same rows: the memoized sidebar is handed the same array.
    expect(result.current.history).toBe(before);

    // Turn end counts as activity and moves it to the top.
    act(() => result.current.patchConversation("b", { messages: [msg("final")] }));
    expect(result.current.history.map((c) => c.id)).toEqual(["b", "a"]);
  });
});
