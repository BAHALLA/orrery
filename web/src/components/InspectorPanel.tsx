import type { ActivityEntry, TriageResponse } from "../api/types";
import type { SystemController } from "../system/useSystem";
import { AgentsPanel } from "./AgentsPanel";
import { CloseIcon } from "./icons";
import { SystemPanel } from "./SystemPanel";
import { ToolCallsTable } from "./ToolCallsTable";
import { TriageReport } from "./TriageReport";

export type InspectorTab = "tools" | "triage" | "system" | "agents";

interface Props {
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  onClose: () => void;
  activity: ActivityEntry[];
  triage: TriageResponse | null;
  system: SystemController;
}

/** Right-hand inspector: the tool-call table and the triage report, kept out
 * of the chat column so the conversation stays clean. */
export function InspectorPanel({ tab, onTab, onClose, activity, triage, system }: Props) {
  const tabClass = (active: boolean) =>
    `px-3 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors focus-visible:outline-none focus-visible:text-brand-600 ${
      active
        ? "border-brand-600 text-brand-700 dark:text-brand-300 dark:border-brand-400"
        : "border-transparent text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
    }`;

  return (
    // A drawer over the conversation below `xl`, a docked column from `xl` up.
    <aside
      aria-label="Inspector"
      className="absolute inset-y-0 right-0 z-20 flex w-full max-w-md flex-col border-l border-slate-200 bg-white shadow-2xl shadow-slate-900/20 xl:static xl:z-auto xl:w-96 xl:max-w-none xl:shrink-0 xl:shadow-none dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="flex items-center justify-between border-b border-slate-200 px-2 dark:border-slate-800">
        <div className="flex" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "tools"}
            className={tabClass(tab === "tools")}
            onClick={() => onTab("tools")}
          >
            Tool calls
            {activity.length > 0 ? (
              <span className="ml-1.5 rounded-full bg-slate-200 px-1.5 text-xs tabular-nums text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                {activity.length}
              </span>
            ) : null}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "triage"}
            className={tabClass(tab === "triage")}
            onClick={() => onTab("triage")}
          >
            Triage
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "system"}
            className={tabClass(tab === "system")}
            onClick={() => onTab("system")}
          >
            System
            {system.selfTest && !system.selfTest.ok ? (
              <span
                className="ml-1.5 rounded-full bg-red-100 px-1.5 text-xs font-semibold text-red-700 dark:bg-red-950 dark:text-red-300"
                title="Some environment checks failed"
              >
                !
              </span>
            ) : null}
          </button>
          {/* Admins only — and the server enforces it; this only hides the tab. */}
          {system.me?.role === "admin" ? (
            <button
              type="button"
              role="tab"
              aria-selected={tab === "agents"}
              className={tabClass(tab === "agents")}
              onClick={() => onTab("agents")}
            >
              Agents
            </button>
          ) : null}
        </div>
        <button
          type="button"
          aria-label="Close inspector"
          className="rounded p-1.5 text-slate-400 hover:bg-slate-200 hover:text-slate-700 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          onClick={onClose}
        >
          <CloseIcon />
        </button>
      </div>
      <div className="orrery-scroll min-h-0 flex-1 overflow-y-auto">
        {tab === "tools" ? (
          <ToolCallsTable entries={activity} />
        ) : tab === "system" ? (
          <SystemPanel system={system} />
        ) : tab === "agents" ? (
          <AgentsPanel system={system} />
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
