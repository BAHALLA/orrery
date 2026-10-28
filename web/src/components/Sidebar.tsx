import { memo, useRef } from "react";
import type { MeResponse } from "../api/types";
import type { Identity } from "../auth/token";
import type { ConversationSummary } from "../conversations/useConversations";
import type { ConsoleView } from "./ChatConsole";
import {
  AgentsIcon,
  CloseIcon,
  PlusIcon,
  RadarIcon,
  ShieldIcon,
  StudioIcon,
  TrashIcon,
} from "./icons";
import { joinLabels, resolveSpecialists, systemLabels } from "../specialists";
import { BREAKPOINT_LG, useFocusTrap, useMediaQuery } from "./a11y";
import { IdentityBadge } from "./IdentityBadge";
import { OrreryMark } from "./OrreryMark";
import { ThemeToggle } from "./ThemeToggle";

interface Props {
  /** History rows, newest first (stable while a reply streams). */
  history: ConversationSummary[];
  activeId: string;
  /** True while the history list is being fetched for the first time. */
  isLoading: boolean;
  /** Why history is incomplete, if it is. */
  error: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  identity: Identity | null;
  me: MeResponse | null;
  isSending: boolean;
  /** Drawer state on narrow screens; ignored from `lg` up, where the rail is static. */
  open: boolean;
  onClose: () => void;
  onNewChat: () => void;
  onRunTriage: () => void;
  onOpenSystem: () => void;
  /** Which page the main column shows; "agents" and "studio" are admin pages. */
  view: ConsoleView;
  /** The Agents page holds unsaved edits: marked here so they aren't forgotten. */
  agentsDirty: boolean;
  onOpenAgents: () => void;
  onOpenStudio: () => void;
  /** Decisions a Studio run waits for: a person is needed there. */
  studioWaiting: number;
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
function historyLabel(c: ConversationSummary): string {
  if (c.draft) return "empty";
  return c.updatedAt > 0 ? relativeTime(c.updatedAt) : "—";
}

const secondaryButton =
  "flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-100 hover:text-slate-900 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none disabled:opacity-50 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white";

/** Left rail: brand, primary actions, conversation history, identity footer.
 * Memoized: every prop is stable while an answer streams. */
export const Sidebar = memo(function Sidebar({
  history: list,
  activeId,
  isLoading,
  error,
  onSelect: selectConversation,
  onDelete: deleteConversation,
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
  onOpenStudio,
  studioWaiting,
  onShowChat,
  onSignOut,
}: Props) {
  // Below `lg` the rail is a drawer: modal while open (focus moves in and
  // stays), and inert while closed so Tab can't wander into it off-screen.
  const wide = useMediaQuery(BREAKPOINT_LG);
  const drawer = !wide;
  const ref = useRef<HTMLElement>(null);
  useFocusTrap(ref, drawer && open);

  const systems = systemLabels(resolveSpecialists(me?.specialists));
  const sweepTitle = systems.length
    ? `Run a full health sweep across ${joinLabels(systems)}`
    : "Run a full health sweep across every system";

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
        ref={ref}
        aria-label="Navigation"
        inert={drawer && !open}
        {...(drawer && open ? { role: "dialog", "aria-modal": true } : {})}
        className={`fixed inset-y-0 left-0 z-40 flex w-72 shrink-0 flex-col border-r border-slate-200 bg-white transition-transform duration-200 lg:static lg:z-auto lg:translate-x-0 dark:border-slate-800 dark:bg-slate-900 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center gap-2.5 px-4 py-4">
          <OrreryMark className="h-7 w-7 text-slate-900 dark:text-slate-100" />
          <span className="font-semibold tracking-tight text-slate-900 dark:text-slate-100">
            Orrery <span className="font-normal text-slate-500 dark:text-slate-400">Console</span>
          </span>
          <button
            type="button"
            aria-label="Close menu"
            onClick={onClose}
            className="ml-auto rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-700 lg:hidden dark:hover:bg-slate-800 dark:hover:text-slate-200"
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
            title={sweepTitle}
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
                <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] font-medium text-warn-fg">
                  <span className="h-1.5 w-1.5 rounded-full bg-warn-solid" aria-hidden="true" />
                  unsaved
                </span>
              ) : null}
            </button>
          ) : null}
          {me?.role === "admin" ? (
            <button
              type="button"
              aria-current={view === "studio" ? "page" : undefined}
              className={`${secondaryButton} ${
                view === "studio"
                  ? "bg-brand-50 text-brand-900 hover:bg-brand-50 dark:bg-brand-950/60 dark:text-brand-100 dark:hover:bg-brand-950/60"
                  : ""
              }`}
              onClick={then(onOpenStudio)}
              title="Compose agents, remote A2A agents and MCP tools, and run them apart from the live system"
            >
              <StudioIcon /> Studio
              {studioWaiting ? (
                <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] font-medium text-warn-fg">
                  <span
                    className="h-1.5 w-1.5 rounded-full bg-warn-solid motion-safe:animate-pulse"
                    aria-hidden="true"
                  />
                  {studioWaiting} waiting for you
                </span>
              ) : (
                <span className="ml-auto rounded-full bg-slate-100 px-1.5 py-px text-[10px] font-semibold tracking-wide text-slate-600 uppercase dark:bg-slate-800 dark:text-slate-300">
                  preview
                </span>
              )}
            </button>
          ) : null}
        </div>

        <nav
          aria-label="Conversation history"
          className="orrery-scroll relative mt-5 min-h-0 flex-1 overflow-y-auto px-2"
        >
          <p className="px-2 pb-1.5 text-[11px] font-semibold tracking-wider text-slate-500 uppercase dark:text-slate-400">
            History
          </p>
          {isLoading ? (
            <p className="px-2 py-1 text-xs text-slate-500 dark:text-slate-400">Loading…</p>
          ) : null}
          {error ? (
            <p role="status" className="px-2 py-1 text-xs text-warn-fg">
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
                    <span className="block text-[11px] text-slate-500 dark:text-slate-400">
                      {historyLabel(c)}
                    </span>
                  </button>
                  <button
                    type="button"
                    aria-label={`Delete conversation: ${c.title}`}
                    onClick={() => deleteConversation(c.id)}
                    className="absolute top-2 right-1.5 rounded-md p-1 text-slate-500 opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 hover:bg-crit-bg hover:text-crit-fg focus:opacity-100 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none pointer-coarse:opacity-100"
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
});
