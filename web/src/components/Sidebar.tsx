import type { MeResponse } from "../api/types";
import type { Identity } from "../auth/token";
import type { Conversation } from "../conversations/types";
import type { ConversationsController } from "../conversations/useConversations";
import { AgentsIcon, CloseIcon, PlusIcon, RadarIcon, ShieldIcon, TrashIcon } from "./icons";
import { IdentityBadge } from "./IdentityBadge";
import { OrreryMark } from "./OrreryMark";
import { ThemeToggle } from "./ThemeToggle";

interface Props {
  conversations: ConversationsController;
  identity: Identity | null;
  me: MeResponse | null;
  isSending: boolean;
  /** Drawer state on narrow screens; ignored from `lg` up, where the rail is static. */
  open: boolean;
  onClose: () => void;
  onNewChat: () => void;
  onRunTriage: () => void;
  onOpenSystem: () => void;
  /** Which page the main column shows; "agents" is the admin dashboard. */
  view: "chat" | "agents";
  /** The Agents page holds unsaved edits: marked here so they aren't forgotten. */
  agentsDirty: boolean;
  onOpenAgents: () => void;
  /** Called when a history row is opened, so the main column shows the chat. */
  onShowChat: () => void;
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

const secondaryButton =
  "flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-100 hover:text-slate-900 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none disabled:opacity-50 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white";

/** Left rail: brand, primary actions, conversation history, identity footer. */
export function Sidebar({
  conversations,
  identity,
  me,
  isSending,
  open,
  onClose,
  onNewChat,
  onRunTriage,
  onOpenSystem,
  view,
  agentsDirty,
  onOpenAgents,
  onShowChat,
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

  // On a narrow screen any navigation also closes the drawer.
  const then =
    <A extends unknown[]>(fn: (...args: A) => void) =>
    (...args: A) => {
      fn(...args);
      onClose();
    };

  return (
    <>
      {/* Scrim behind the drawer on narrow screens. */}
      <div
        aria-hidden="true"
        onClick={onClose}
        className={`fixed inset-0 z-30 bg-slate-950/40 backdrop-blur-sm transition-opacity lg:hidden ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-72 shrink-0 flex-col border-r border-slate-200 bg-white transition-transform duration-200 lg:static lg:z-auto lg:translate-x-0 dark:border-slate-800 dark:bg-slate-900 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center gap-2.5 px-4 py-4">
          <OrreryMark className="h-7 w-7 text-slate-900 dark:text-slate-100" />
          <span className="font-semibold tracking-tight text-slate-900 dark:text-slate-100">
            Orrery <span className="font-normal text-slate-400 dark:text-slate-500">Console</span>
          </span>
          <button
            type="button"
            aria-label="Close menu"
            onClick={onClose}
            className="ml-auto rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 lg:hidden dark:hover:bg-slate-800 dark:hover:text-slate-200"
          >
            <CloseIcon />
          </button>
        </div>

        <div className="flex flex-col gap-1 px-3">
          <button
            type="button"
            className="mb-1 flex items-center justify-center gap-1.5 rounded-lg bg-brand-600 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-brand-500 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:outline-none dark:focus-visible:ring-offset-slate-900"
            onClick={then(onNewChat)}
          >
            <PlusIcon /> New chat
          </button>
          <button
            type="button"
            disabled={isSending}
            className={secondaryButton}
            onClick={then(onRunTriage)}
            title="Run a full health sweep across Kafka, K8s, Docker, Observability, and Elasticsearch"
          >
            <RadarIcon /> Run triage
          </button>
          <button
            type="button"
            className={secondaryButton}
            onClick={then(onOpenSystem)}
            title="Check which integrations are wired and what this deployment will let you do"
          >
            <ShieldIcon /> Check my environment
          </button>
          {/* Admins only — and the server enforces it; this only hides the entry. */}
          {me?.role === "admin" ? (
            <button
              type="button"
              aria-current={view === "agents" ? "page" : undefined}
              className={`${secondaryButton} ${
                view === "agents"
                  ? "bg-brand-50 text-brand-900 hover:bg-brand-50 dark:bg-brand-950/60 dark:text-brand-100 dark:hover:bg-brand-950/60"
                  : ""
              }`}
              onClick={then(onOpenAgents)}
              title="Which agents and tools the coordinator can reach, and their guards"
            >
              <AgentsIcon /> Agents{" "}
              {agentsDirty ? (
                <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] font-medium text-amber-600 dark:text-amber-400">
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden="true" />
                  unsaved
                </span>
              ) : null}
            </button>
          ) : null}
        </div>

        <nav
          aria-label="Conversation history"
          className="orrery-scroll mt-5 min-h-0 flex-1 overflow-y-auto px-2"
        >
          <p className="px-2 pb-1.5 text-[11px] font-semibold tracking-wider text-slate-400 uppercase dark:text-slate-500">
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
                    onClick={then(() => {
                      selectConversation(c.id);
                      onShowChat();
                    })}
                    aria-current={active && view === "chat" ? "page" : undefined}
                    className={`w-full rounded-lg px-2.5 py-2 pr-8 text-left transition-colors focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none ${
                      active && view === "chat"
                        ? "bg-brand-50 text-brand-900 dark:bg-brand-950/60 dark:text-brand-100"
                        : "text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800/60"
                    }`}
                  >
                    <span className="block truncate text-sm">{c.title}</span>
                    <span className="block text-[11px] text-slate-400 dark:text-slate-500">
                      {historyLabel(c)}
                    </span>
                  </button>
                  <button
                    type="button"
                    aria-label={`Delete conversation: ${c.title}`}
                    onClick={() => void deleteConversation(c.id)}
                    className="absolute top-2 right-1.5 rounded-md p-1 text-slate-400 opacity-0 group-hover:opacity-100 hover:bg-red-50 hover:text-red-600 focus:opacity-100 dark:hover:bg-red-950/50"
                  >
                    <TrashIcon size={14} />
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="flex flex-col gap-3 border-t border-slate-200 p-3 dark:border-slate-800">
          <IdentityBadge identity={identity} me={me} onSignOut={onSignOut} />
          <ThemeToggle />
        </div>
      </aside>
    </>
  );
}
