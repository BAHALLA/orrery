import { useCallback, useEffect, useRef, useState } from "react";
import type { Identity } from "../auth/token";
import { flatten } from "../chat/trace";
import { useChat } from "../chat/useChat";
import { useConversations } from "../conversations/useConversations";
import { AgentsDashboard } from "./AgentsDashboard";
import { ConfirmationPanel } from "./ConfirmationPanel";
import { ActivityIcon, CloseIcon, MenuIcon, OrbitSpinner, RadarIcon } from "./icons";
import { InspectorPanel, type InspectorTab } from "./InspectorPanel";
import { MessageInput } from "./MessageInput";
import { MessageList } from "./MessageList";
import { severityBadge } from "./severity";
import { Sidebar } from "./Sidebar";
import { useSystem } from "../system/useSystem";

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
  const [view, setView] = useState<"chat" | "agents">("chat");
  // The Agents page mounts on first visit and then stays mounted (hidden), so
  // an unsaved catalog draft survives a trip back to the chat.
  const [agentsOpened, setAgentsOpened] = useState(false);
  const [agentsDirty, setAgentsDirty] = useState(false);
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

  const showChat = () => {
    setView("chat");
    setVerdictWaiting(false);
  };

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

  const toggleInspector = (tab: InspectorTab) => {
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
  };

  // The reply being written, if any — drives the live status in the header.
  // A streaming reply is always the newest message: nothing is appended after it.
  const last = chat.messages.at(-1);
  const live = last?.streaming ? last : undefined;
  const liveSteps = live?.trace ? flatten(live.trace).length : 0;

  return (
    <div className="flex h-dvh bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <Sidebar
        conversations={conversations}
        identity={identity}
        me={system.me}
        isSending={chat.isSending}
        open={navOpen}
        onClose={() => setNavOpen(false)}
        onNewChat={() => {
          showChat();
          conversations.newConversation();
        }}
        onRunTriage={() => {
          showChat();
          void chat.runTriage();
        }}
        onOpenSystem={() => toggleInspector("system")}
        view={view}
        agentsDirty={agentsDirty}
        onOpenAgents={() => {
          setAgentsOpened(true);
          setView("agents");
        }}
        onShowChat={showChat}
        onSignOut={onSignOut}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-2 border-b border-slate-200/80 bg-white/80 px-3 py-2.5 backdrop-blur sm:px-4 dark:border-slate-800/80 dark:bg-slate-900/80">
          <button
            type="button"
            aria-label="Open menu"
            onClick={() => setNavOpen(true)}
            className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 lg:hidden dark:hover:bg-slate-800"
          >
            <MenuIcon size={18} />
          </button>
          <h2 className="min-w-0 flex-1 truncate font-medium text-slate-800 dark:text-slate-100">
            {view === "agents" ? "Agents" : conversations.active.title}
          </h2>
          {live ? (
            <span
              role="status"
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
        {view === "chat" ? (
          <div className="relative flex min-h-0 flex-1">
            <section className="flex min-w-0 flex-1 flex-col">
              <MessageList
                messages={chat.messages}
                isSending={chat.isSending}
                onSuggest={(prompt) => void chat.send(prompt)}
                onSweep={() => void chat.runTriage()}
              />

              {chat.pending ? (
                <ConfirmationPanel
                  pending={chat.pending}
                  disabled={chat.isSending}
                  onDecide={(word) => void chat.decide(word)}
                />
              ) : null}

              {chat.error ? (
                <div className="px-3 sm:px-6">
                  <div
                    role="alert"
                    className="mx-auto mb-2 flex w-full max-w-3xl items-center justify-between gap-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/50 dark:text-red-300"
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
                onSend={(text) => void chat.send(text)}
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
    </div>
  );
}
