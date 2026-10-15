import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { ApiClient, ApiError } from "../api/client";
import type { SessionMessage, SessionSummary } from "../api/types";
import type { ChatMessage } from "../chat/types";
import { legacyStorageKeys } from "../config";
import { describeApiError } from "../system/useSystem";
import { NEW_CONVERSATION_TITLE, type Conversation } from "./types";

let seq = 0;
function newId(): string {
  seq += 1;
  return `c-${Date.now()}-${seq}`;
}

/** A conversation that exists only in the browser, until its first turn is sent. */
function emptyConversation(): Conversation {
  return {
    id: newId(),
    sessionId: null,
    title: NEW_CONVERSATION_TITLE,
    messages: [],
    updatedAt: Date.now(),
    loaded: true,
  };
}

/** True for a "New chat" entry the user hasn't used yet. */
function isUntouchedDraft(c: Conversation): boolean {
  return c.sessionId === null && c.messages.length === 0;
}

/** The conversation to fall back to when the active one goes away: the most
 * recently updated, which is also the one the sidebar shows at the top. The
 * list is not held in display order, so this cannot be `list[0]`. */
function mostRecent(list: Conversation[]): Conversation {
  return list.reduce((best, c) => (c.updatedAt > best.updatedAt ? c : best), list[0]);
}

/** A listed session, as a sidebar row: title and time, transcript not yet fetched. */
function fromSummary(summary: SessionSummary): Conversation {
  return {
    id: summary.session_id,
    sessionId: summary.session_id,
    title: summary.title || NEW_CONVERSATION_TITLE,
    messages: [],
    // The server's clock, in seconds; the console works in epoch ms. A 0 means
    // the store had no timestamp — kept as 0 so the sidebar can omit the age
    // rather than claim the conversation just happened.
    updatedAt: summary.last_update_time > 0 ? summary.last_update_time * 1000 : 0,
    loaded: false,
  };
}

function toChatMessages(sessionId: string, messages: SessionMessage[]): ChatMessage[] {
  return messages.map((m, i) => ({
    id: `${sessionId}:${i}`,
    role: m.role,
    text: m.text,
    at: m.at * 1000,
  }));
}

/**
 * Drop the conversation history the console used to keep in localStorage.
 *
 * Transcripts now live in the session store, so these keys are dead — but a
 * browser that used an earlier build still holds a full copy of past
 * conversations on disk, which nothing would ever read or clear again.
 */
function purgeLegacyStorage(): void {
  try {
    legacyStorageKeys.forEach((key) => localStorage.removeItem(key));
  } catch {
    // storage unavailable (private mode) — nothing to purge
  }
}

interface ConversationsState {
  list: Conversation[];
  activeId: string;
}

type ConversationPatch =
  | Partial<Omit<Conversation, "id">>
  | ((current: Conversation) => Partial<Omit<Conversation, "id">>);

/** Every mutation carries what it needs, so the reducer stays a pure function
 * of (state, action) — no `Date.now()`, no id generation, nothing that would
 * differ between StrictMode's two invocations. */
type ConversationsAction =
  | { type: "new"; fresh: Conversation }
  | { type: "select"; id: string }
  | { type: "delete"; id: string; fallback: Conversation }
  | { type: "patch"; patch: ConversationPatch; at: number }
  | { type: "patchById"; id: string; patch: ConversationPatch; at: number }
  | { type: "hydrate"; listed: Conversation[] }
  | { type: "transcript"; id: string; title: string; messages: ChatMessage[] };

/**
 * The list and the active selection move together, in one reducer.
 *
 * They are a single piece of state: creating, deleting, selecting and hydrating
 * all change both at once. Held as two `useState`s they could only be updated by
 * either reading the list from the render closure (a stale snapshot — an update
 * queued earlier in the same tick, such as an arriving assistant reply, gets
 * overwritten) or by calling `setActiveId` inside the list updater (an impure
 * reducer, double-invoked under StrictMode). A reducer has neither problem:
 * React replays every dispatch against the freshest state.
 */
function reducer(state: ConversationsState, action: ConversationsAction): ConversationsState {
  switch (action.type) {
    case "new": {
      // Reuse an existing empty conversation instead of piling up blanks.
      const blank = state.list.find(isUntouchedDraft);
      if (blank) return { ...state, activeId: blank.id };
      return { list: [action.fresh, ...state.list], activeId: action.fresh.id };
    }
    case "select":
      return state.activeId === action.id ? state : { ...state, activeId: action.id };
    case "delete": {
      const next = state.list.filter((c) => c.id !== action.id);
      if (next.length === 0) return { list: [action.fallback], activeId: action.fallback.id };
      return {
        list: next,
        activeId: state.activeId === action.id ? mostRecent(next).id : state.activeId,
      };
    }
    case "patch":
    case "patchById": {
      const target = action.type === "patch" ? state.activeId : action.id;
      const list = state.list.map((c) => {
        if (c.id !== target) return c;
        const delta = typeof action.patch === "function" ? action.patch(c) : action.patch;
        return { ...c, ...delta, updatedAt: action.at };
      });
      return { ...state, list };
    }
    case "hydrate": {
      // Local entries win over the listing. A turn sent while `/sessions` was in
      // flight has a transcript the summary doesn't carry, and re-adding the
      // server's row for the same session would duplicate the conversation.
      const local = state.list.filter((c) => !isUntouchedDraft(c));
      const known = new Set(local.map((c) => c.sessionId));
      const merged = [...local, ...action.listed.filter((c) => !known.has(c.sessionId))];
      if (merged.length === 0) {
        // Nothing stored and nothing local: keep the draft that was already here.
        return state;
      }
      // The untouched draft is dropped when there is real history to show —
      // otherwise every load opens on a blank "New chat" above the conversation
      // the operator was actually in, which is the one to reopen.
      const activeId = merged.some((c) => c.id === state.activeId)
        ? state.activeId
        : mostRecent(merged).id;
      return { list: merged, activeId };
    }
    case "transcript": {
      const list = state.list.map((c) =>
        c.id === action.id
          ? { ...c, title: action.title || c.title, messages: action.messages, loaded: true }
          : c,
      );
      return { ...state, list };
    }
  }
}

export interface ConversationsController {
  /** Newest first. */
  conversations: Conversation[];
  activeId: string;
  active: Conversation;
  /** True while the history list is being fetched for the first time. */
  isLoading: boolean;
  /** Why history is incomplete, when a fetch failed in a way worth showing. */
  error: string | null;
  newConversation: () => void;
  selectConversation: (id: string) => void;
  /** Deletes server-side first, so the row only disappears once it is really gone. */
  deleteConversation: (id: string) => Promise<void>;
  /** Merge a partial update into the active conversation (bumps updatedAt).
   * Accepts an updater so async callers always read the freshest transcript. */
  patchActive: (patch: ConversationPatch) => void;
  /** Like {@link patchActive}, but for a named conversation — for writes that
   * arrive asynchronously (a streamed reply) and must land on the conversation
   * that asked, even if the user has since switched to another. */
  patchConversation: (id: string, patch: ConversationPatch) => void;
}

/**
 * Owns the conversation history (list + active selection), backed by the server.
 *
 * The session store is the source of truth: `GET /sessions` rebuilds the sidebar
 * on any browser and `GET /session/{id}` fetches a transcript when its
 * conversation is opened. The console keeps nothing in localStorage, so history
 * follows the user between machines instead of being stranded in whichever
 * browser happened to create it — and a long thread is no longer silently
 * truncated to fit a storage quota.
 *
 * Transcripts load lazily, one conversation at a time: the listing carries
 * titles only, so opening the console costs one request regardless of how much
 * history the user has.
 */
export function useConversations(token: string | null): ConversationsController {
  const client = useMemo(() => new ApiClient(token), []); // eslint-disable-line react-hooks/exhaustive-deps
  const [state, dispatch] = useReducer(reducer, undefined, () => {
    const fresh = emptyConversation();
    return { list: [fresh], activeId: fresh.id };
  });
  const { list, activeId } = state;
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Which transcripts are already being fetched, so switching back and forth
  // between two conversations doesn't queue a request per click.
  const inFlight = useRef<Set<string>>(new Set());

  useEffect(() => {
    client.setToken(token);
  }, [client, token]);

  const active = useMemo(() => list.find((c) => c.id === activeId) ?? list[0], [list, activeId]);

  // Load the history list once, then open whatever conversation ends up active.
  useEffect(() => {
    let cancelled = false;
    purgeLegacyStorage();
    void (async () => {
      try {
        const res = await client.sessions();
        if (cancelled) return;
        dispatch({ type: "hydrate", listed: (res.sessions ?? []).map(fromSummary) });
      } catch (err) {
        if (cancelled) return;
        // A console with no history is still a usable console: the draft stays,
        // and the next turn opens a real session.
        setError(describeApiError(err, "Could not load your conversation history."));
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);

  // Fetch the active conversation's transcript whenever it is missing — covers
  // both the initial hydrate and every later selection, without either path
  // having to remember to ask.
  //
  // No cancel-on-cleanup flag here, unlike the listing above: this effect
  // re-runs on every change to the active conversation, including an arriving
  // reply, so cancelling would abandon a transcript fetch mid-flight and leave
  // the conversation permanently unloaded (the in-flight guard would suppress
  // the retry). The `inFlight` set is what prevents duplicate requests, and a
  // dispatch after unmount is a no-op.
  useEffect(() => {
    const { id, sessionId, loaded } = active;
    if (sessionId === null || loaded || inFlight.current.has(sessionId)) return;
    inFlight.current.add(sessionId);
    void (async () => {
      try {
        const res = await client.session(sessionId);
        dispatch({
          type: "transcript",
          id,
          title: res.title,
          messages: toChatMessages(sessionId, res.messages ?? []),
        });
      } catch (err) {
        // Left unloaded on purpose: reopening it retries, which is the only
        // recovery a transient failure needs.
        setError(describeApiError(err, "Could not load that conversation."));
      } finally {
        inFlight.current.delete(sessionId);
      }
    })();
  }, [active, client]);

  const newConversation = useCallback(
    () => dispatch({ type: "new", fresh: emptyConversation() }),
    [],
  );

  const selectConversation = useCallback((id: string) => dispatch({ type: "select", id }), []);

  const deleteConversation = useCallback(
    async (id: string) => {
      const target = list.find((c) => c.id === id);
      // A draft was never stored; there is nothing to delete but the row.
      if (target?.sessionId) {
        try {
          await client.deleteSession(target.sessionId);
        } catch (err) {
          // 404 means it is already gone — the row should still disappear.
          if (!(err instanceof ApiError && err.status === 404)) {
            setError(describeApiError(err, "Could not delete that conversation."));
            return;
          }
        }
      }
      dispatch({ type: "delete", id, fallback: emptyConversation() });
    },
    [client, list],
  );

  const patchActive = useCallback(
    (patch: ConversationPatch) => dispatch({ type: "patch", patch, at: Date.now() }),
    [],
  );

  const patchConversation = useCallback(
    (id: string, patch: ConversationPatch) =>
      dispatch({ type: "patchById", id, patch, at: Date.now() }),
    [],
  );

  const sorted = useMemo(() => [...list].sort((a, b) => b.updatedAt - a.updatedAt), [list]);

  return {
    conversations: sorted,
    activeId,
    active,
    isLoading,
    error,
    newConversation,
    selectConversation,
    deleteConversation,
    patchConversation,
    patchActive,
  };
}
