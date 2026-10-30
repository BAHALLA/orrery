import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Identity } from "../auth/token";
import { flatten } from "../chat/trace";
import { useChat } from "../chat/useChat";
import { useConversations } from "../conversations/useConversations";
import { useDeferredDelete } from "../conversations/useDeferredDelete";
import { AgentsDashboard } from "./agents/AgentsDashboard";
import { BREAKPOINT_LG, useMediaQuery, useTurnAnnouncement } from "./a11y";
import { ShowApprovalContext } from "./approval";
import { ConfirmationPanel } from "./ConfirmationPanel";
import { pendingPlacement } from "../chat/pending";
import { ActivityIcon, CloseIcon, MenuIcon, OrbitSpinner, RadarIcon, ShieldIcon } from "./icons";
import { InspectorPanel, type InspectorTab } from "./InspectorPanel";
import { MessageInput } from "./MessageInput";
import { MessageList } from "./MessageList";
import { severityBadge, toneChip } from "./severity";
import { Sidebar } from "./Sidebar";
import { useStoredFlag } from "./storedFlag";
import { storageKeys } from "../config";
import { resolveSpecialists } from "../specialists";
import { useSystem } from "../system/useSystem";

// The Studio (AEP-031) is admin-only and brings React Flow with it: loaded on
// first visit, so none of it is in the chat console's first load.
const StudioView = lazy(() => import("../studio/StudioView"));

/** The page title (index.html); a Studio run waiting for a person prefixes it. */
const BASE_TITLE = "Orrery Console";

export type ConsoleView = "chat" | "agents" | "studio";

interface Props {
  token: string;
  identity: Identity | null;
  onSignOut: () => void;
}

const headerButton = (active: boolean) =>
  `inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none ${
    active
      ? "bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-white"
      : "text-slate-500 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-white"
  }`;

/** The authenticated console: sidebar + conversation column + inspector. */
export function ChatConsole({ token, identity, onSignOut }: Props) {
  const conversations = useConversations(token);
  const chat = useChat(token, conversations);
  const system = useSystem(token);

  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("tools");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [navCollapsed, setNavCollapsed] = useStoredFlag(storageKeys.navCollapsed);
  const toggleNavCollapsed = useCallback(() => setNavCollapsed((c) => !c), [setNavCollapsed]);
  const [requestedView, setView] = useState<ConsoleView>("chat");
  // A Studio the server does not offer (switched off, or a role change) is
  // never shown, whatever was asked for: the chat stands in for it.
  const studioAvailable = system.me?.studio_available === true;
  const view: ConsoleView = requestedView === "studio" && !studioAvailable ? "chat" : requestedView;
  // The Agents page mounts on first visit and then stays mounted (hidden), so
  // an unsaved catalog draft survives a trip back to the chat.
  const [agentsOpened, setAgentsOpened] = useState(false);
  const [agentsDirty, setAgentsDirty] = useState(false);
  // Like the Agents page, the Studio stays mounted once opened: its canvas and
  // this session's runs survive a trip to the chat.
  const [studioOpened, setStudioOpened] = useState(false);
  // Decisions a Studio run waits for, shown in the sidebar and the tab title:
  // an approval left unseen expires and ends the run rejected.
  const [studioWaiting, setStudioWaiting] = useState(0);
  useEffect(() => {
    document.title = studioWaiting
      ? `(${studioWaiting}) Approval needed · ${BASE_TITLE}`
      : BASE_TITLE;
  }, [studioWaiting]);
  useEffect(
    () => () => {
      document.title = BASE_TITLE;
    },
    [],
  );
  // Read by the triage effect, which must not re-run when only this changes.
  const agentsDirtyRef = useRef(false);
  const onAgentsDirty = useCallback((dirty: boolean) => {
    agentsDirtyRef.current = dirty;
    setAgentsDirty(dirty);
  }, []);
  // A verdict that arrived while the admin was mid-edit on the Agents page.
  const [verdictWaiting, setVerdictWaiting] = useState(false);

  // Surface a fresh triage verdict without the user hunting for it — but never
  // by pulling an admin off a page with unsaved edits; badge it there instead.
  const lastSeverity = useRef<string | null>(null);
  useEffect(() => {
    const sev = chat.triage?.severity ?? null;
    if (sev && sev !== lastSeverity.current) {
      setInspectorTab("triage");
      setInspectorOpen(true);
      if (agentsDirtyRef.current) setVerdictWaiting(true);
      else setView("chat");
    }
    lastSeverity.current = sev;
  }, [chat.triage]);

  const showChat = useCallback(() => {
    setView("chat");
    setVerdictWaiting(false);
  }, []);

  // Ctrl/Cmd+B folds the rail, as in most editors. Only where it folds (below
  // `lg` it is a drawer), by key position so any layout has it, and not under
  // a dialog, which owns the keyboard.
  const wide = useMediaQuery(BREAKPOINT_LG);
  useEffect(() => {
    if (!wide) return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || e.code !== "KeyB") return;
      e.preventDefault();
      if (e.repeat || document.querySelector('[aria-modal="true"]')) return;
      toggleNavCollapsed();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [wide, toggleNavCollapsed]);

  // Escape closes whichever overlay is on top.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (navOpen) setNavOpen(false);
      else if (inspectorOpen) setInspectorOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen, inspectorOpen]);

  const toggleInspector = useCallback(
    (tab: InspectorTab) => {
      if (view !== "chat") {
        showChat();
        setInspectorTab(tab);
        setInspectorOpen(true);
      } else if (inspectorOpen && inspectorTab === tab) {
        setInspectorOpen(false);
      } else {
        setInspectorTab(tab);
        setInspectorOpen(true);
      }
    },
    [view, inspectorOpen, inspectorTab, showChat],
  );

  // Stable handlers: the sidebar and transcript are memoized, and a new
  // closure per render would redraw them on every streamed frame.
  const { newConversation, selectConversation, deleteConversation } = conversations;
  const { send, runTriage } = chat;
  const closeNav = useCallback(() => setNavOpen(false), []);
  const onNewChat = useCallback(() => {
    showChat();
    newConversation();
  }, [showChat, newConversation]);
  const onRunTriage = useCallback(() => {
    showChat();
    void runTriage();
  }, [showChat, runTriage]);
  const onOpenSystem = useCallback(() => toggleInspector("system"), [toggleInspector]);
  const onOpenAgents = useCallback(() => {
    setAgentsOpened(true);
    setView("agents");
  }, []);
  const onOpenStudio = useCallback(() => {
    if (!studioAvailable) return;
    setStudioOpened(true);
    setView("studio");
  }, [studioAvailable]);
  // Deleting is held for a few seconds behind an Undo (the store delete is
  // permanent). The row hides at once; if it was the open conversation, a new
  // chat takes its place, and Undo brings it back.
  const deleteNow = useCallback((id: string) => void deleteConversation(id), [deleteConversation]);
  const deferred = useDeferredDelete(deleteNow);
  const { schedule: scheduleDelete, undo: cancelDelete } = deferred;
  const restoreOnUndo = useRef<string | null>(null);
  const { history, activeId } = conversations;
  const onDelete = useCallback(
    (id: string) => {
      const title = history.find((c) => c.id === id)?.title ?? "conversation";
      restoreOnUndo.current = id === activeId ? id : null;
      if (id === activeId) newConversation();
      scheduleDelete(id, title);
    },
    [history, activeId, newConversation, scheduleDelete],
  );
  const undoDelete = useCallback(() => {
    cancelDelete();
    if (restoreOnUndo.current) selectConversation(restoreOnUndo.current);
    restoreOnUndo.current = null;
  }, [cancelDelete, selectConversation]);
  const pendingDeleteId = deferred.pending?.id ?? null;
  const visibleHistory = useMemo(
    () => (pendingDeleteId ? history.filter((c) => c.id !== pendingDeleteId) : history),
    [history, pendingDeleteId],
  );
  const onSuggest = useCallback((prompt: string) => void send(prompt), [send]);
  const onSweep = useCallback(() => void runTriage(), [runTriage]);

  // The pending action is per user; show its card only in the conversation
  // it is waiting in, and a pointer to that conversation everywhere else.
  const placement = pendingPlacement(chat.pending, chat.sessionId);
  const waitingIn =
    placement === "elsewhere"
      ? conversations.history.find((c) => c.sessionId === chat.pending?.session_id)
      : undefined;

  // The Approve/Deny card, for the transcript's "awaiting approval" chips.
  const approvalRef = useRef<HTMLElement>(null);
  const showApproval = useCallback(() => {
    const panel = approvalRef.current;
    if (!panel) return;
    panel.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
    panel.focus({ preventScroll: true });
  }, []);

  const specialists = useMemo(() => resolveSpecialists(system.me?.specialists), [system.me]);
  const announcement = useTurnAnnouncement(chat.isSending, chat.error !== null);

  // The reply being written, if any — drives the live status in the header.
  // A streaming reply is always the newest message: nothing is appended after it.
  const last = chat.messages.at(-1);
  const live = last?.streaming ? last : undefined;
  const liveSteps = live?.trace ? flatten(live.trace).length : 0;

  return (
    <div className="relative flex h-dvh overflow-hidden bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <Sidebar
        history={visibleHistory}
        activeId={conversations.activeId}
        isLoading={conversations.isLoading}
        error={conversations.error}
        onSelect={selectConversation}
        onDelete={onDelete}
        identity={identity}
        me={system.me}
        isSending={chat.isSending}
        open={navOpen}
        onClose={closeNav}
        collapsed={navCollapsed}
        onToggleCollapsed={toggleNavCollapsed}
        onNewChat={onNewChat}
        onRunTriage={onRunTriage}
        onOpenSystem={onOpenSystem}
        view={view}
        agentsDirty={agentsDirty}
        onOpenAgents={onOpenAgents}
        onOpenStudio={onOpenStudio}
        studioWaiting={studioWaiting}
        onShowChat={showChat}
        onSignOut={onSignOut}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        {/* The one live region for a turn: "working", then "answer ready". */}
        <p role="status" className="sr-only">
          {announcement}
        </p>
        {/* Its own region, so a turn's announcements never drown it out. */}
        <p role="status" className="sr-only">
          {studioWaiting
            ? `${studioWaiting} Studio decision${studioWaiting === 1 ? "" : "s"} waiting for you.`
            : ""}
        </p>
        {/* The Studio's own toolbar shows the page: from `lg` up this row would
            only repeat it, and the canvas needs the height. It stays for
            screen readers, as the page's heading. */}
        <header
          className={`flex items-center gap-2 border-b border-slate-200/80 bg-white/80 px-3 py-2.5 backdrop-blur sm:px-4 dark:border-slate-800/80 dark:bg-slate-900/80 ${view === "studio" ? "lg:sr-only" : ""}`}
        >
          <button
            type="button"
            aria-label="Open menu"
            onClick={() => setNavOpen(true)}
            className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 lg:hidden dark:hover:bg-slate-800"
          >
            <MenuIcon size={18} />
          </button>
          <h2 className="min-w-0 flex-1 truncate font-medium text-slate-800 dark:text-slate-100">
            {view === "agents"
              ? "Agents"
              : view === "studio"
                ? "Studio"
                : conversations.active.title}
          </h2>
          {live ? (
            <span
              aria-hidden="true"
              className="hidden items-center gap-1.5 rounded-full bg-brand-50 px-2.5 py-1 text-xs font-medium text-brand-700 sm:inline-flex dark:bg-brand-950/60 dark:text-brand-200"
            >
              <OrbitSpinner />
              Working{liveSteps ? ` · ${liveSteps} step${liveSteps === 1 ? "" : "s"}` : ""}
            </span>
          ) : null}
          {view === "agents" && verdictWaiting && chat.triage?.severity ? (
            <button
              type="button"
              className={headerButton(false)}
              onClick={() => toggleInspector("triage")}
            >
              <RadarIcon />
              Triage verdict ready
              <span
                className={`rounded-full px-1.5 py-0.5 text-[11px] font-semibold uppercase ${severityBadge[chat.triage.severity]}`}
              >
                {chat.triage.severity}
              </span>
            </button>
          ) : null}
          {view === "chat" ? (
            <>
              <button
                type="button"
                className={headerButton(inspectorOpen && inspectorTab === "tools")}
                onClick={() => toggleInspector("tools")}
              >
                <ActivityIcon />
                <span className="sr-only sm:not-sr-only">Tool calls</span>
                {chat.activity.length > 0 ? (
                  <span className="rounded-full bg-slate-200 px-1.5 text-xs text-slate-600 tabular-nums dark:bg-slate-700 dark:text-slate-300">
                    {chat.activity.length}
                  </span>
                ) : null}
              </button>
              <button
                type="button"
                className={headerButton(inspectorOpen && inspectorTab === "triage")}
                onClick={() => toggleInspector("triage")}
              >
                <RadarIcon />
                <span className="sr-only sm:not-sr-only">Triage</span>
                {chat.triage?.severity ? (
                  <span
                    className={`rounded-full px-1.5 py-0.5 text-[11px] font-semibold uppercase ${severityBadge[chat.triage.severity]}`}
                  >
                    {chat.triage.severity}
                  </span>
                ) : null}
              </button>
            </>
          ) : null}
        </header>

        {agentsOpened ? (
          <div className={view === "agents" ? "flex min-h-0 flex-1 flex-col" : "hidden"}>
            <AgentsDashboard
              system={system}
              active={view === "agents"}
              onDirtyChange={onAgentsDirty}
            />
          </div>
        ) : null}
        {studioOpened && token ? (
          <div className={view === "studio" ? "flex min-h-0 flex-1 flex-col" : "hidden"}>
            <Suspense
              fallback={
                <p className="p-6 text-sm text-slate-500 dark:text-slate-400">
                  Loading the Studio…
                </p>
              }
            >
              <StudioView token={token} active={view === "studio"} onAttention={setStudioWaiting} />
            </Suspense>
          </div>
        ) : null}
        {view === "chat" ? (
          <div className="relative flex min-h-0 flex-1">
            <section className="flex min-w-0 flex-1 flex-col">
              <ShowApprovalContext.Provider value={placement === "here" ? showApproval : null}>
                <MessageList
                  messages={chat.messages}
                  isSending={chat.isSending}
                  onSuggest={onSuggest}
                  onSweep={onSweep}
                  specialists={specialists}
                  autonomy={system.me?.autonomy_level ?? null}
                />
              </ShowApprovalContext.Provider>

              {placement === "here" && chat.pending ? (
                <ConfirmationPanel
                  panelRef={approvalRef}
                  pending={chat.pending}
                  disabled={chat.isSending}
                  onDecide={(word) => void chat.decide(word)}
                />
              ) : null}

              {placement === "elsewhere" && chat.pending ? (
                <div className="px-3 sm:px-6">
                  <p
                    role="status"
                    className={`mx-auto mb-2 flex w-full max-w-3xl flex-wrap items-center gap-x-2 gap-y-1 rounded-xl border px-3 py-2 text-sm ${toneChip.warn}`}
                  >
                    <ShieldIcon size={15} />
                    <span className="min-w-0">
                      An action is waiting for your approval in{" "}
                      <span className="font-medium">
                        {waitingIn ? waitingIn.title : "another conversation"}
                      </span>
                    </span>
                    {waitingIn ? (
                      <button
                        type="button"
                        aria-label={`Open ${waitingIn.title}`}
                        className="ml-auto font-medium underline underline-offset-2 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none"
                        onClick={() => {
                          showChat();
                          selectConversation(waitingIn.id);
                        }}
                      >
                        Open <span aria-hidden="true">→</span>
                      </button>
                    ) : null}
                  </p>
                </div>
              ) : null}

              {chat.error ? (
                <div className="px-3 sm:px-6">
                  <div
                    role="alert"
                    className="mx-auto mb-2 flex w-full max-w-3xl items-center justify-between gap-3 rounded-xl border border-crit-border bg-crit-bg px-3 py-2 text-sm text-crit-fg"
                  >
                    <span className="min-w-0">{chat.error.message}</span>
                    <span className="flex shrink-0 items-center gap-3">
                      {chat.error.isAuth ? (
                        <button type="button" className="font-medium underline" onClick={onSignOut}>
                          Re-enter token
                        </button>
                      ) : chat.error.canRetry ? (
                        <button
                          type="button"
                          className="font-medium underline disabled:opacity-50"
                          disabled={chat.isSending}
                          onClick={() => void chat.retry()}
                        >
                          Retry
                        </button>
                      ) : null}
                      <button
                        type="button"
                        aria-label="Dismiss error"
                        className="rounded p-0.5 opacity-60 hover:opacity-100"
                        onClick={chat.clearError}
                      >
                        <CloseIcon size={14} />
                      </button>
                    </span>
                  </div>
                </div>
              ) : null}

              <MessageInput
                disabled={chat.isSending}
                isSending={chat.isSending}
                attachments={system.me?.attachments ?? null}
                onSend={(text, files) => void chat.send(text, files)}
                onStop={chat.stop}
              />
            </section>

            {inspectorOpen ? (
              <>
                {/* Below xl the inspector overlays the conversation as a drawer. */}
                <div
                  aria-hidden="true"
                  onClick={() => setInspectorOpen(false)}
                  className="absolute inset-0 z-10 bg-slate-950/30 backdrop-blur-[1px] xl:hidden"
                />
                <InspectorPanel
                  tab={inspectorTab}
                  onTab={setInspectorTab}
                  onClose={() => setInspectorOpen(false)}
                  activity={chat.activity}
                  triage={chat.triage}
                  system={system}
                />
              </>
            ) : null}
          </div>
        ) : null}
      </main>

      {deferred.pending ? (
        <div
          role="status"
          className="fixed bottom-4 left-1/2 z-50 flex w-[min(28rem,calc(100%-2rem))] -translate-x-1/2 items-center gap-3 rounded-xl bg-slate-900 px-4 py-2.5 text-sm text-white shadow-xl shadow-slate-900/30 lg:left-4 lg:translate-x-0 dark:bg-slate-800"
        >
          <span className="min-w-0 flex-1 truncate">
            Deleted <span className="font-medium">{deferred.pending.title}</span>
          </span>
          <button
            type="button"
            onClick={undoDelete}
            className="rounded-md px-2 py-1 font-semibold text-brand-200 hover:bg-white/10 focus-visible:ring-2 focus-visible:ring-brand-300 focus-visible:outline-none"
          >
            Undo
          </button>
          <button
            type="button"
            aria-label="Dismiss and delete now"
            onClick={deferred.commit}
            className="rounded-md p-1 text-slate-300 hover:bg-white/10 hover:text-white focus-visible:ring-2 focus-visible:ring-brand-300 focus-visible:outline-none"
          >
            <CloseIcon size={14} />
          </button>
        </div>
      ) : null}
    </div>
  );
}
