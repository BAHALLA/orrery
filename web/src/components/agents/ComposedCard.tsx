import type { CatalogResponse, DeclarativeAgent } from "../../api/types";
import { guardCounts } from "../agentDraft";
import { guardDot } from "../guards";
import { pendingBadge, pendingBorder } from "./styles";

/** One composed agent in the draft, marked new or edited until saved. */
export function ComposedCard({
  agent,
  saved,
  tools,
  onEdit,
  onDuplicate,
  onRemove,
}: {
  agent: DeclarativeAgent;
  saved: DeclarativeAgent | undefined;
  tools: CatalogResponse["tools"];
  onEdit: () => void;
  onDuplicate: () => void;
  onRemove: () => void;
}) {
  const counts = guardCounts(agent.tools, tools);
  const state = !saved ? "new" : JSON.stringify(saved) !== JSON.stringify(agent) ? "edited" : null;
  const SHOWN = 6;

  return (
    <li
      className={`flex flex-col rounded-xl border bg-white p-4 dark:bg-slate-900 ${
        state ? pendingBorder : "border-slate-200 dark:border-slate-800"
      }`}
    >
      <div className="flex items-center gap-2">
        <p className="font-mono text-sm font-medium text-slate-900 dark:text-slate-100">
          {agent.name}
        </p>
        {state ? <span className={pendingBadge}>{state} · unsaved</span> : null}
      </div>
      <p
        className="mt-1 line-clamp-2 text-xs text-slate-500 dark:text-slate-400"
        title={agent.description}
      >
        {agent.description}
      </p>
      <ul className="mt-2 flex flex-wrap gap-1" aria-label={`${agent.name} tools`}>
        {agent.tools.slice(0, SHOWN).map((ref) => (
          <li
            key={ref}
            className="rounded-md border border-slate-200 px-1.5 py-0.5 font-mono text-[11px] text-slate-600 dark:border-slate-700 dark:text-slate-300"
          >
            {ref}
          </li>
        ))}
        {agent.tools.length > SHOWN ? (
          <li className="px-1 py-0.5 text-[11px] text-slate-500">
            +{agent.tools.length - SHOWN} more
          </li>
        ) : null}
      </ul>
      <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
        <span className="inline-flex items-center gap-2 text-slate-500">
          {agent.tools.length} tool{agent.tools.length === 1 ? "" : "s"}
          {counts.confirm ? (
            <span className="inline-flex items-center gap-1" title="Need approval">
              <span className={`h-1.5 w-1.5 rounded-full ${guardDot.confirm}`} />
              {counts.confirm}
            </span>
          ) : null}
          {counts.destructive ? (
            <span className="inline-flex items-center gap-1" title="Destructive">
              <span className={`h-1.5 w-1.5 rounded-full ${guardDot.destructive}`} />
              {counts.destructive}
            </span>
          ) : null}
        </span>
        <button
          type="button"
          className="ml-auto font-medium text-brand-600 hover:underline dark:text-brand-400"
          onClick={onEdit}
        >
          Edit
        </button>
        <button
          type="button"
          aria-label={`Duplicate agent ${agent.name}`}
          className="font-medium text-slate-600 hover:underline dark:text-slate-300"
          onClick={onDuplicate}
        >
          Duplicate
        </button>
        <button
          type="button"
          aria-label={`Remove agent ${agent.name}`}
          className="font-medium text-crit-fg hover:underline"
          onClick={onRemove}
        >
          Remove
        </button>
      </div>
    </li>
  );
}
