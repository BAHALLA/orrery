import type { MeResponse } from "../api/types";
import type { Identity } from "../auth/token";
import type { Conversation } from "../conversations/types";
import type { ConversationsController } from "../conversations/useConversations";
import { IdentityBadge } from "./IdentityBadge";
import { OrreryMark } from "./OrreryMark";

interface Props {
  conversations: ConversationsController;
  identity: Identity | null;
  me: MeResponse | null;
  isSending: boolean;
  onNewChat: () => void;
  onRunTriage: () => void;
  onOpenSystem: () => void;
  onSignOut: () => void;
}

function relativeTime(ts: number): string {
  const secs = Math.round((Date.now() - ts) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

/** The second line of a history row.
 *
 * A stored conversation is dated even before its transcript is fetched, so the
 * age comes from the listing rather than from `messages` — which is empty until
 * the conversation is opened. Only an unused draft is "empty". */
function historyLabel(c: Conversation): string {
  if (c.sessionId === null && c.messages.length === 0) return "empty";
  return c.updatedAt > 0 ? relativeTime(c.updatedAt) : "—";
}

/** Left rail: brand, primary actions, conversation history, identity footer. */
export function Sidebar({
  conversations,
  identity,
  me,
  isSending,
  onNewChat,
  onRunTriage,
  onOpenSystem,
  onSignOut,
}: Props) {
  const {
    conversations: list,
    activeId,
    isLoading,
    error,
    selectConversation,
    deleteConversation,
  } = conversations;

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
      <div className="flex items-center gap-2 px-4 py-4">
        <OrreryMark className="h-6 w-6 text-slate-900 dark:text-slate-100" />
        <span className="font-semibold text-slate-900 dark:text-slate-100">Orrery Console</span>
      </div>

      <div className="flex flex-col gap-2 px-3">
        <button
          type="button"
          className="flex items-center justify-center gap-1.5 rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700"
          onClick={onNewChat}
        >
          <span aria-hidden="true">＋</span> New chat
        </button>
        <button
          type="button"
          disabled={isSending}
          className="flex items-center justify-center gap-1.5 rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
          onClick={onRunTriage}
          title="Run a full health sweep across Kafka, K8s, Docker, Observability, and Elasticsearch"
        >
          Run triage
        </button>
        <button
          type="button"
          className="flex items-center justify-center gap-1.5 rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
          onClick={onOpenSystem}
          title="Check which integrations are wired and what this deployment will let you do"
        >
          Check my environment
        </button>
      </div>

      <nav
        aria-label="Conversation history"
        className="orrery-scroll mt-4 min-h-0 flex-1 overflow-y-auto px-2"
      >
        <p className="px-2 pb-1 text-xs font-semibold uppercase tracking-wide text-slate-400 dark:text-slate-500">
          History
        </p>
        {isLoading ? (
          <p className="px-2 py-1 text-xs text-slate-400 dark:text-slate-500">Loading…</p>
        ) : null}
        {error ? (
          <p role="status" className="px-2 py-1 text-xs text-amber-600 dark:text-amber-400">
            {error}
          </p>
        ) : null}
        <ul className="flex flex-col gap-0.5">
          {list.map((c) => {
            const active = c.id === activeId;
            return (
              <li key={c.id} className="group relative">
                <button
                  type="button"
                  onClick={() => selectConversation(c.id)}
                  className={`w-full rounded-lg px-2 py-2 pr-7 text-left ${
                    active
                      ? "bg-slate-100 dark:bg-slate-800"
                      : "hover:bg-slate-50 dark:hover:bg-slate-800/50"
                  }`}
                >
                  <span className="block truncate text-sm text-slate-800 dark:text-slate-200">
                    {c.title}
                  </span>
                  <span className="block text-xs text-slate-400 dark:text-slate-500">
                    {historyLabel(c)}
                  </span>
                </button>
                <button
                  type="button"
                  aria-label={`Delete conversation: ${c.title}`}
                  onClick={() => void deleteConversation(c.id)}
                  className="absolute top-2 right-1 rounded p-1 text-slate-400 opacity-0 group-hover:opacity-100 hover:bg-slate-200 hover:text-red-600 focus:opacity-100 dark:hover:bg-slate-700"
                >
                  🗑
                </button>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="border-t border-slate-200 p-3 dark:border-slate-800">
        <IdentityBadge identity={identity} me={me} onSignOut={onSignOut} />
      </div>
    </aside>
  );
}
