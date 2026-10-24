import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import type { ActivityEntry, TriageResponse } from "../api/types";
import type { SystemController } from "../system/useSystem";
import { BREAKPOINT_XL, useFocusTrap, useMediaQuery } from "./a11y";
import { CloseIcon } from "./icons";
import { SystemPanel } from "./SystemPanel";
import { ToolCallsTable } from "./ToolCallsTable";
import { TriageReport } from "./TriageReport";

export type InspectorTab = "tools" | "triage" | "system";

const TABS: readonly InspectorTab[] = ["tools", "triage", "system"];

interface Props {
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  onClose: () => void;
  activity: ActivityEntry[];
  triage: TriageResponse | null;
  system: SystemController;
}

/** Right-hand inspector: the tool-call table and the triage report, kept out
 * of the chat column so the conversation stays clean.
 *
 * Below `xl` it is a drawer over the conversation, so it behaves like a modal
 * dialog: focus moves in, Tab stays in, and closing gives focus back. From `xl`
 * up it is a docked column and none of that applies. The tabs follow the WAI-ARIA
 * tabs pattern: one tab stop, arrow keys (and Home/End) move between tabs. */
export function InspectorPanel({ tab, onTab, onClose, activity, triage, system }: Props) {
  const docked = useMediaQuery(BREAKPOINT_XL);
  const ref = useRef<HTMLElement>(null);
  useFocusTrap(ref, !docked);

  const id = useId();
  const tabId = (t: InspectorTab) => `${id}-tab-${t}`;
  const panelId = `${id}-panel`;
  const tabRefs = useRef<Partial<Record<InspectorTab, HTMLButtonElement | null>>>({});

  const onTabKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const index = TABS.indexOf(tab);
    const next =
      e.key === "ArrowRight"
        ? TABS[(index + 1) % TABS.length]
        : e.key === "ArrowLeft"
          ? TABS[(index - 1 + TABS.length) % TABS.length]
          : e.key === "Home"
            ? TABS[0]
            : e.key === "End"
              ? TABS[TABS.length - 1]
              : null;
    if (!next) return;
    e.preventDefault();
    onTab(next);
    tabRefs.current[next]?.focus();
  };

  const tabClass = (active: boolean) =>
    `px-3 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-500 ${
      active
        ? "border-brand-600 text-brand-700 dark:text-brand-300 dark:border-brand-400"
        : "border-transparent text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
    }`;

  const tabButton = (t: InspectorTab, label: ReactNode) => (
    <button
      key={t}
      ref={(el) => {
        tabRefs.current[t] = el;
      }}
      type="button"
      role="tab"
      id={tabId(t)}
      aria-selected={tab === t}
      aria-controls={panelId}
      tabIndex={tab === t ? 0 : -1}
      className={tabClass(tab === t)}
      onClick={() => onTab(t)}
    >
      {label}
    </button>
  );

  return (
    <aside
      ref={ref}
      aria-label="Inspector"
      {...(docked ? {} : { role: "dialog", "aria-modal": true })}
      tabIndex={-1}
      className="absolute inset-y-0 right-0 z-20 flex w-full max-w-md flex-col border-l border-slate-200 bg-white shadow-2xl shadow-slate-900/20 focus:outline-none xl:static xl:z-auto xl:w-96 xl:max-w-none xl:shrink-0 xl:shadow-none dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-2 dark:border-slate-800">
        <div className="flex" role="tablist" aria-label="Inspector views" onKeyDown={onTabKey}>
          {tabButton(
            "tools",
            <>
              Tool calls
              {activity.length > 0 ? (
                <span className="ml-1.5 rounded-full bg-slate-200 px-1.5 text-xs tabular-nums text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                  {activity.length}
                </span>
              ) : null}
            </>,
          )}
          {tabButton("triage", "Triage")}
          {tabButton(
            "system",
            <>
              System
              {system.selfTest && !system.selfTest.ok ? (
                <span
                  className="ml-1.5 rounded-full bg-crit-bg px-1.5 text-xs font-semibold text-crit-fg"
                  title="Some environment checks failed"
                >
                  !<span className="sr-only"> (some checks failed)</span>
                </span>
              ) : null}
            </>,
          )}
        </div>
        <button
          type="button"
          aria-label="Close inspector"
          className="rounded p-1.5 text-slate-500 hover:bg-slate-200 hover:text-slate-700 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:hover:bg-slate-800 dark:hover:text-slate-200"
          onClick={onClose}
        >
          <CloseIcon />
        </button>
      </div>
      <div
        role="tabpanel"
        id={panelId}
        aria-labelledby={tabId(tab)}
        tabIndex={0}
        className="orrery-scroll relative min-h-0 flex-1 overflow-y-auto focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-500 focus-visible:outline-none"
      >
        {tab === "tools" ? (
          <ToolCallsTable entries={activity} />
        ) : tab === "system" ? (
          <SystemPanel system={system} />
        ) : triage ? (
          <TriageReport triage={triage} activity={activity} />
        ) : (
          <p className="p-4 text-sm text-slate-500 dark:text-slate-400">
            No triage verdict yet. Click <span className="font-medium">Run triage</span> in the
            sidebar.
          </p>
        )}
      </div>
    </aside>
  );
}
