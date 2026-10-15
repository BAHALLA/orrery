import { useEffect } from "react";
import type { ToolGuard } from "../api/types";
import type { SystemController } from "../system/useSystem";

interface Props {
  system: SystemController;
}

const guardStyle: Record<ToolGuard, string> = {
  read: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  confirm: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  destructive: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  dynamic: "bg-brand-100 text-brand-700 dark:bg-brand-950 dark:text-brand-300",
};

const guardTitle: Record<ToolGuard, string> = {
  read: "Read-only — any role",
  confirm: "Changes state — operator, after approval",
  destructive: "Destructive — admin, after approval",
  dynamic: "MCP toolset — each tool classified when discovered",
};

const sourceLabel: Record<string, string> = {
  builtin: "built-in",
  plugin: "plugin",
  declarative: "declarative",
  mcp: "MCP",
};

/**
 * What the coordinator can reach, for admins: each specialist, where it came
 * from, and every tool with its guard level. Read straight from the running
 * agent tree, so a disabled specialist is absent here because it is absent
 * there.
 */
export function AgentsPanel({ system }: Props) {
  const { agents, agentsError, loadAgents } = system;

  useEffect(() => {
    if (agents === null) void loadAgents();
  }, [agents, loadAgents]);

  if (agentsError) {
    return (
      <p
        role="alert"
        className="m-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/50 dark:text-red-300"
      >
        {agentsError}
      </p>
    );
  }
  if (agents === null) {
    return <p className="p-4 text-sm text-slate-500 dark:text-slate-400">Loading agents…</p>;
  }

  return (
    <div className="flex flex-col gap-3 p-4">
      <p className="text-xs text-slate-500 dark:text-slate-400">
        {agents.length} agent{agents.length === 1 ? "" : "s"} the coordinator can delegate to.
        Enable or disable them with <code>ORRERY_SPECIALISTS</code>, add your own in{" "}
        <code>ORRERY_AGENTS_CONFIG</code>.
      </p>
      <ul className="flex flex-col gap-3" aria-label="Agents">
        {agents.map((a) => (
          <li
            key={a.name}
            className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900"
          >
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm font-medium text-slate-800 dark:text-slate-100">
                {a.name}
              </span>
              <span className="ml-auto rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                {sourceLabel[a.source] ?? a.source}
              </span>
            </div>
            {a.description ? (
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{a.description}</p>
            ) : null}
            <ul className="mt-2 flex flex-wrap gap-1" aria-label={`${a.name} tools`}>
              {a.tools.map((t) => (
                <li
                  key={t.name}
                  title={`${guardTitle[t.guard]}${t.reason ? ` — ${t.reason}` : ""}`}
                  className={`rounded px-1.5 py-0.5 font-mono text-[11px] ${guardStyle[t.guard]}`}
                >
                  {t.name}
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}
