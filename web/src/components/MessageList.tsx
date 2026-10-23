import { memo, useEffect, useRef } from "react";
import { splitAttachments } from "../chat/attachments";
import { decisionOf } from "../chat/decision";
import type { ChatMessage, Decision } from "../chat/types";
import { AttachmentChip } from "./AttachmentChip";
import { AgentMarkdown } from "./AgentMarkdown";
import { CopyButton } from "./CopyButton";
import { CheckIcon, CloseIcon } from "./icons";
import { OrreryMark } from "./OrreryMark";
import { RunTrace } from "./RunTrace";
import { toneChip } from "./severity";
import { Welcome } from "./Welcome";

interface Props {
  messages: ChatMessage[];
  isSending: boolean;
  /** Send a suggested prompt from the empty state. */
  onSuggest: (prompt: string) => void;
  onSweep: () => void;
}

function formatTime(at: number): string {
  return new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** How far from the bottom still counts as "following along" (px). */
const STICK_THRESHOLD = 80;

// Memoized on the message object: a streamed delta replaces only the reply
// being written, so every other message keeps its identity and is skipped.
const UserMessage = memo(function UserMessage({ m }: { m: ChatMessage }) {
  // A message sent from here carries its files; a reloaded one carries them as
  // the server's placeholder lines, turned back into chips here.
  const parsed = m.attachments
    ? { text: m.text, attachments: m.attachments }
    : splitAttachments(m.text);
  return (
    <div className="group flex flex-col items-end gap-1">
      <div className="flex max-w-[85%] flex-col gap-2 rounded-2xl rounded-br-md bg-brand-600 px-4 py-2.5 text-white shadow-sm sm:max-w-[75%]">
        {parsed.attachments.length ? (
          <ul className="flex flex-wrap justify-end gap-1.5" aria-label="Attached files">
            {parsed.attachments.map((a, i) => (
              <li key={`${a.name}-${i}`} className="min-w-0">
                <AttachmentChip attachment={a} tone="bubble" />
              </li>
            ))}
          </ul>
        ) : null}
        {parsed.text ? <p className="whitespace-pre-wrap break-words">{parsed.text}</p> : null}
      </div>
      <div className="flex items-center gap-1.5 px-1 text-xs text-slate-500 dark:text-slate-400">
        <time dateTime={new Date(m.at).toISOString()}>{formatTime(m.at)}</time>
        <span className="opacity-0 transition group-focus-within:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100">
          <CopyButton value={parsed.text} label="Copy the message" />
        </span>
      </div>
    </div>
  );
});

/**
 * An approve/deny click, as a compact line rather than a one-word bubble:
 * "You approved restart_container (orders-worker) · 10:05". It records what
 * the user said, not what happened — the server's gate decides that, and the
 * reply below says how it went.
 */
const DecisionRow = memo(function DecisionRow({
  m,
  decision,
}: {
  m: ChatMessage;
  decision: Decision;
}) {
  const approved = decision.word === "approve";
  return (
    <div className="flex justify-center">
      <p
        className={`inline-flex max-w-full flex-wrap items-center justify-center gap-x-1.5 rounded-full border px-3 py-1 text-xs ${
          approved ? toneChip.ok : toneChip.neutral
        }`}
      >
        {approved ? (
          <CheckIcon size={13} aria-hidden="true" />
        ) : (
          <CloseIcon size={13} aria-hidden="true" />
        )}
        <span>
          You {approved ? "approved" : "denied"}
          {decision.tool ? (
            <>
              {" "}
              <span className="font-mono font-medium">{decision.tool}</span>
            </>
          ) : (
            " the pending action"
          )}
          {decision.target ? <span className="break-all"> ({decision.target})</span> : null}
        </span>
        <span aria-hidden="true">·</span>
        <time dateTime={new Date(m.at).toISOString()}>{formatTime(m.at)}</time>
      </p>
    </div>
  );
});

const AssistantMessage = memo(function AssistantMessage({ m }: { m: ChatMessage }) {
  const live = m.streaming === true;
  return (
    <article className="group flex gap-3" aria-busy={live}>
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-900 text-white ring-1 ring-slate-900/5 dark:bg-slate-800">
        <OrreryMark className="h-5 w-5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
          <span className="font-semibold text-slate-700 dark:text-slate-200">Orrery</span>
          <time dateTime={new Date(m.at).toISOString()}>{formatTime(m.at)}</time>
          {m.text && !live ? (
            <span className="opacity-0 transition group-focus-within:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100">
              <CopyButton value={m.text} label="Copy the reply" />
            </span>
          ) : null}
        </div>
        {m.trace ? <RunTrace trace={m.trace} live={live} durationMs={m.durationMs} /> : null}
        {m.text ? (
          <div className={live ? "orrery-caret" : undefined}>
            <AgentMarkdown text={m.text} />
          </div>
        ) : null}
        {m.interrupted ? (
          <p className="mt-2 text-xs text-slate-500 italic dark:text-slate-400">
            Stopped before the answer arrived. The server keeps working — reopen this conversation
            in a moment to read it.
          </p>
        ) : null}
      </div>
    </article>
  );
});

/**
 * Scrolling transcript. Follows the newest message *only while the reader is
 * already at the bottom* — scrolling up to re-read an earlier answer while a
 * multi-specialist sweep is still landing must not yank them back down.
 *
 * Assistant replies render as full-width documents rather than chat bubbles:
 * they are reports with tables and code, and a bubble squeezed them into a
 * column too narrow to read either.
 */
export function MessageList({ messages, isSending, onSuggest, onSweep }: Props) {
  const endRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD;
  };

  // Smooth scrolling restarts its animation on every call; fired once per
  // streamed frame it never settles and fights the reader. While an answer is
  // being written the jump is instant; a new message still glides into view.
  const streaming = messages.at(-1)?.streaming === true;
  useEffect(() => {
    if (!stickToBottom.current) return;
    endRef.current?.scrollIntoView?.({ behavior: streaming ? "auto" : "smooth", block: "end" });
  }, [messages, isSending, streaming]);

  if (messages.length === 0 && !isSending) {
    return <Welcome onSuggest={onSuggest} onSweep={onSweep} />;
  }

  return (
    <div
      onScroll={handleScroll}
      className="orrery-scroll flex-1 overflow-y-auto"
      // A log, but not a chatty one: it is busy while an answer is written, so
      // assistive tech holds the stream instead of reading every token. The
      // console's status line announces "working" and "answer ready".
      role="log"
      aria-label="Conversation transcript"
      aria-busy={streaming}
    >
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6 sm:px-6">
        {messages.map((m) => {
          if (m.role === "assistant") return <AssistantMessage key={m.id} m={m} />;
          const decision = decisionOf(m);
          return decision ? (
            <DecisionRow key={m.id} m={m} decision={decision} />
          ) : (
            <UserMessage key={m.id} m={m} />
          );
        })}
        <div ref={endRef} />
      </div>
    </div>
  );
}
