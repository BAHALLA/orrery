import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiClient, ApiError } from "../api/client";
import type { ActivityEntry, PendingConfirmation, TriageResponse } from "../api/types";
import type { ConversationsController } from "../conversations/useConversations";
import { NEW_CONVERSATION_TITLE } from "../conversations/types";
import { refsFor } from "./attachments";
import { createFrameBatcher } from "./frameBatcher";
import { applyProgress } from "./trace";
import { decisionFor } from "./decision";
import type { ChatError, ChatMessage, Decision } from "./types";

let idCounter = 0;
function nextId(): string {
  idCounter += 1;
  return `${Date.now()}-${idCounter}`;
}

/** The canned prompt behind the "Run triage" button — routes to the
 * incident_triage_agent AgentTool on the chat root. */
export const TRIAGE_PROMPT =
  "Run a full incident triage across all systems and report the verdict.";

/** First line of the first user message, trimmed for the sidebar. */
function titleFrom(text: string): string {
  const line = text.split("\n")[0].trim();
  return line.length > 48 ? `${line.slice(0, 47)}…` : line || NEW_CONVERSATION_TITLE;
}

export interface ChatController {
  messages: ChatMessage[];
  sessionId: string | null;
  isSending: boolean;
  error: ChatError | null;
  /** Tool calls recorded for this session (the inspector table). */
  activity: ActivityEntry[];
  /** The caller's guarded action awaiting approve/deny, if any. */
  pending: PendingConfirmation | null;
  /** The session's latest triage verdict, if one was recorded. */
  triage: TriageResponse | null;
  /** Send a turn; *files* are attachments (AEP-030), shown to the model once. */
  send: (text: string, files?: File[]) => Promise<void>;
  /** Send an approve/deny decision through the normal chat flow. */
  decide: (word: "approve" | "deny") => Promise<void>;
  /** Kick off a full triage sweep (the canned prompt). */
  runTriage: () => Promise<void>;
  /** Abandon the in-flight turn. The server keeps working; the UI stops waiting. */
  stop: () => void;
  /** Re-send the message whose turn failed, if any. */
  retry: () => Promise<void>;
  /** Dismiss the current error without retrying. */
  clearError: () => void;
}

/**
 * Drives one conversation against the /chat endpoint.
 *
 * The transcript and the server session id live in the active conversation
 * (owned by {@link useConversations}); this hook owns the request lifecycle
 * and the transient inspector state (activity / pending / triage), resetting
 * and reloading them whenever the active conversation changes.
 */
export function useChat(token: string | null, conv: ConversationsController): ChatController {
  const client = useMemo(() => new ApiClient(token), []); // eslint-disable-line react-hooks/exhaustive-deps
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<ChatError | null>(null);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [triage, setTriage] = useState<TriageResponse | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // The turn in flight (or the one that just failed), so a retry can replay it
  // — files included — without the user redoing anything: the composer clears
  // on send.
  const lastSentRef = useRef<{ text: string; files: File[] } | null>(null);

  const { active, activeId, patchConversation } = conv;
  const messages = active.messages;
  const sessionId = active.sessionId;

  // Which conversation the UI is showing *right now*, tracked synchronously so
  // an in-flight side-pane refresh can tell if the user has since switched away.
  const activeIdRef = useRef(activeId);
  // Written in an effect, not during render. Every reader is an async
  // side-pane callback that lands long after effects have flushed, so the
  // value it sees is unchanged — and a ref write during render is exactly the
  // impurity that breaks under StrictMode double-invocation.
  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);

  // Keep the client's token current without recreating it (preserves identity).
  useEffect(() => {
    client.setToken(token);
  }, [client, token]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  // Best-effort inspector panes: the timeline, pending-confirmation, and
  // triage state are rendering data, so a failed refresh never surfaces as a
  // chat error.
  const refreshSidePanes = useCallback(
    async (sessionId: string, forConversation: string) => {
      // The caller names the conversation this session belongs to. If the user
      // switches away while the requests are in flight, discard the results —
      // otherwise a slow response for the previous conversation clobbers the
      // inspector of the one now on screen. (Reading the *current* active id
      // here instead would compare it against itself and never fire.)
      const [act, pend, tri] = await Promise.allSettled([
        client.activity(sessionId),
        client.pendingConfirmation(),
        client.triage(sessionId),
      ]);
      if (activeIdRef.current !== forConversation) return;
      if (act.status === "fulfilled") setActivity(act.value.entries ?? []);
      if (pend.status === "fulfilled") setPending(pend.value.pending ?? null);
      if (tri.status === "fulfilled") setTriage(tri.value.severity ? tri.value : null);
    },
    [client],
  );

  // Switching conversations: drop the previous inspector state, then reload
  // this conversation's timeline/triage from the server if it has a session.
  //
  // `set-state-in-effect` is right in general — the idiomatic reset is to key
  // the subtree on the conversation id and let React discard the state. That
  // would mean remounting the pane, which also discards the in-flight abort
  // controller and the stale-refresh guard this hook exists to hold. The race
  // those protect against is the one fixed in 0.2.3/0.3.0 and covered by tests,
  // so the reset stays explicit here rather than being traded for a remount.
  useEffect(() => {
    abortRef.current?.abort();
    /* eslint-disable react-hooks/set-state-in-effect */
    setIsSending(false);
    setError(null);
    setActivity([]);
    setPending(null);
    setTriage(null);
    /* eslint-enable react-hooks/set-state-in-effect */
    lastSentRef.current = null;
    if (sessionId) void refreshSidePanes(sessionId, activeId);
    // Only re-run when the active conversation changes, not on every message.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // No polling while a turn runs: the stream narrates progress itself (the
  // live trace), and the side panes are refreshed once, when the stream says
  // `done` — or when a non-streaming `POST /chat` fallback returns.

  // The latest render's values, for `send` — which is handed out as a stable
  // function so memoized children don't re-render on every streamed delta.
  const latest = useRef({ isSending, sessionId, activeId });
  useEffect(() => {
    latest.current = { isSending, sessionId, activeId };
  }, [isSending, sessionId, activeId]);

  const send = useCallback(
    async (
      raw: string,
      files: File[] = [],
      options: { replay?: boolean; decision?: Decision } = {},
    ) => {
      const text = raw.trim();
      const { isSending, sessionId, activeId } = latest.current;
      if ((!text && !files.length) || isSending) return;
      // Claimed synchronously: a second click in the same tick sees it before
      // the effect above has re-synced the ref from state.
      latest.current = { ...latest.current, isSending: true };

      // The conversation this send belongs to. Every write below names it, so a
      // reply still landing after the user switched away goes to the thread
      // that asked — never into whichever one happens to be on screen.
      const forConversation = activeId;
      // Turn start and end count as activity (they move the conversation to
      // the top of the history); everything streamed in between does not.
      const patch = (fn: Parameters<typeof patchConversation>[1], touch = false) =>
        patchConversation(forConversation, fn, { touch });
      const replyId = nextId();
      const startedAt = Date.now();
      const patchReply = (fn: (m: ChatMessage) => Partial<ChatMessage>, touch = false) =>
        patch(
          (c) => ({
            messages: c.messages.map((m) => (m.id === replyId ? { ...m, ...fn(m) } : m)),
          }),
          touch,
        );
      // Deltas are joined per animation frame: one state update per paint
      // instead of one per token.
      const deltas = createFrameBatcher<string>((chunks) =>
        patchReply((m) => ({ text: m.text + chunks.join("") })),
      );
      let refreshed = false;
      const refreshOnce = (sid: string) => {
        if (refreshed) return Promise.resolve();
        refreshed = true;
        return refreshSidePanes(sid, forConversation);
      };
      let streamSession: string | null = sessionId;

      setError(null);
      // On a retry the message is already in the transcript — appending it
      // again would show the user's question twice for one failed turn.
      const placeholder: ChatMessage = {
        id: replyId,
        role: "assistant",
        text: "",
        at: startedAt,
        trace: [],
        streaming: true,
      };
      patch(
        (c) => ({
          messages: options.replay
            ? [...c.messages, placeholder]
            : [
                ...c.messages,
                {
                  id: nextId(),
                  role: "user",
                  text,
                  at: startedAt,
                  ...(files.length ? { attachments: refsFor(files) } : {}),
                  ...(options.decision ? { decision: options.decision } : {}),
                },
                placeholder,
              ],
          title:
            c.title === NEW_CONVERSATION_TITLE
              ? titleFrom(text || files.map((f) => f.name).join(", "))
              : c.title,
        }),
        true,
      );
      lastSentRef.current = { text, files };
      setIsSending(true);

      const controller = new AbortController();
      abortRef.current = controller;

      // Drop the placeholder if nothing ever arrived, otherwise keep what did —
      // a half-written answer or a trace of what ran is still worth reading.
      const settleUnfinished = () =>
        patch(
          (c) => ({
            messages: c.messages.flatMap((m) => {
              if (m.id !== replyId) return [m];
              if (!m.text && !m.trace?.length) return [];
              return [{ ...m, streaming: false, interrupted: true }];
            }),
          }),
          true,
        );

      try {
        const res = await client.chatStream(
          { message: text, session_id: sessionId, files },
          {
            signal: controller.signal,
            onEvent: (event) => {
              // Anything but a delta lands after the text buffered so far.
              if (event.type !== "delta") deltas.flush();
              switch (event.type) {
                case "session":
                  // Recorded as soon as the server names it: if the stream drops
                  // now, reopening the conversation finds the finished turn.
                  streamSession = event.session_id;
                  patch({ sessionId: event.session_id });
                  break;
                case "progress":
                  patchReply((m) => ({ trace: applyProgress(m.trace ?? [], event.step) }));
                  break;
                case "delta":
                  deltas.push(event.text);
                  break;
                case "message":
                  // Authoritative: replaces whatever the deltas assembled.
                  patchReply(() => ({ text: event.text }));
                  break;
                case "done":
                  // The turn is over server-side: the tool calls, any pending
                  // approval and a triage verdict are all recorded now.
                  if (streamSession) void refreshOnce(streamSession);
                  break;
                case "error":
                  break; // surfaced by chatStream as a thrown ApiError
              }
            },
          },
        );
        deltas.cancel();
        patch({ sessionId: res.session_id });
        patchReply(
          () => ({
            text: res.response,
            streaming: false,
            durationMs: Date.now() - startedAt,
          }),
          true,
        );
        // The non-streaming fallback (and a stream that closed without `done`)
        // never fired the refresh above.
        await refreshOnce(res.session_id);
      } catch (err) {
        deltas.cancel();
        settleUnfinished();
        // Tools may have run before the failure; show what was recorded.
        if (streamSession) void refreshOnce(streamSession);
        // The user moved to another conversation (which aborts this request):
        // the error belongs to a thread no longer on screen, so say nothing.
        if (activeIdRef.current !== forConversation) return;
        // A user-initiated stop is not a failure — say so, and leave the
        // message retryable rather than showing a red error for their own click.
        if (err instanceof DOMException && err.name === "AbortError") {
          setError({
            message: "Stopped. The server may still finish this turn in the background.",
            isAuth: false,
            canRetry: true,
            retryAfter: null,
          });
          return;
        }
        if (err instanceof ApiError) {
          setError({
            message: err.isRateLimited
              ? `Too many requests${err.retryAfter ? ` — retry in ${err.retryAfter}s` : ""}.`
              : err.message,
            isAuth: err.isAuth,
            canRetry: err.isRetryable,
            retryAfter: err.retryAfter,
          });
        } else {
          setError({
            message: "Unexpected error sending message.",
            isAuth: false,
            canRetry: true,
            retryAfter: null,
          });
        }
      } finally {
        setIsSending(false);
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [client, patchConversation, refreshSidePanes],
  );

  // The decision is a plain chat message: the literal word goes to the server,
  // whose requester-verified gate decides whether it authorizes anything. The
  // snapshot of what was pending only changes how the transcript shows it.
  const decide = useCallback(
    (word: "approve" | "deny") => send(word, [], { decision: decisionFor(word, pending) }),
    [send, pending],
  );

  const runTriage = useCallback(() => send(TRIAGE_PROMPT), [send]);

  // Abandoning the request only stops *this* client waiting — the turn carries
  // on server-side, so the wording elsewhere avoids claiming it was cancelled.
  const stop = useCallback(() => abortRef.current?.abort(), []);

  const retry = useCallback(async () => {
    const last = lastSentRef.current;
    if (last) await send(last.text, last.files, { replay: true });
  }, [send]);

  const clearError = useCallback(() => setError(null), []);

  const sendPublic = useCallback((text: string, files?: File[]) => send(text, files), [send]);

  return {
    messages,
    sessionId,
    isSending,
    error,
    activity,
    pending,
    triage,
    send: sendPublic,
    decide,
    runTriage,
    stop,
    retry,
    clearError,
  };
}
