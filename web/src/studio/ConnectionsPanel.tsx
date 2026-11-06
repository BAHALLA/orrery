import { useState } from "react";
import { A2aInspector } from "./A2aInspector";
import { McpInspector } from "./McpInspector";
import type { RunView } from "./runState";
import type { StudioRegistry } from "./types";

type Connection = "a2a" | "mcp";

interface Props {
  registry: StudioRegistry | null;
  view: RunView | null;
  resolving: boolean;
  onResolve: () => void;
}

/**
 * What a run reaches outside itself: remote agents over A2A and tools over MCP,
 * each as its protocol tells it. One tab, because most people composing a run
 * never need either, and those who do are looking for "what did it talk to?".
 */
export function ConnectionsPanel({ registry, view, resolving, onResolve }: Props) {
  // Until someone chooses, whichever the run on show actually used.
  const [chosen, setChosen] = useState<Connection | null>(null);
  const shown: Connection = chosen ?? (!view?.a2a.length && view?.mcp.length ? "mcp" : "a2a");
  const options: { id: Connection; label: string; count: number }[] = [
    { id: "a2a", label: "Remote agents (A2A)", count: view?.a2a.length ?? 0 },
    { id: "mcp", label: "MCP servers", count: view?.mcp.length ?? 0 },
  ];
  return (
    <div className="flex flex-col gap-3">
      <div
        role="group"
        aria-label="Connection type"
        className="flex rounded-lg bg-slate-100 p-0.5 dark:bg-slate-800"
      >
        {options.map(({ id, label, count }) => (
          <button
            key={id}
            type="button"
            aria-pressed={shown === id}
            onClick={() => setChosen(id)}
            className={`flex-1 rounded-md px-2 py-1 text-xs font-medium focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none ${
              shown === id
                ? "bg-white text-slate-900 shadow-sm dark:bg-slate-900 dark:text-white"
                : "text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
            }`}
          >
            {label}
            {count ? <span className="ml-1 tabular-nums">{count}</span> : null}
          </button>
        ))}
      </div>
      {shown === "a2a" ? (
        <A2aInspector registry={registry} view={view} resolving={resolving} onResolve={onResolve} />
      ) : (
        <McpInspector registry={registry} view={view} />
      )}
    </div>
  );
}
