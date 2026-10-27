import { useState } from "react";
import { GUARD_ORDER, guardChip, guardDot, guardLabel, guardTitle } from "../guards";
import { AlertIcon, ShieldIcon } from "../icons";
import { pendingBadge, pendingBorder, pill, sourceLabel } from "./styles";
import type { Card } from "./useAgentsDraft";

/** One agent: its switch, what serves it, and its tools grouped by guard level. */
export function AgentCard({
  card,
  onToggle,
  onCompose,
}: {
  card: Card;
  onToggle?: (() => void) | undefined;
  onCompose?: (() => void) | undefined;
}) {
  const [expanded, setExpanded] = useState(false);
  const groups = GUARD_ORDER.map((g) => ({
    guard: g,
    tools: card.tools.filter((t) => t.guard === g),
  })).filter((g) => g.tools.length > 0);
  const guarded = card.tools.some((t) => t.guard !== "read");
  const pending = card.on !== card.savedOn;
  const meta = card.live;

  return (
    <li
      className={`flex flex-col rounded-xl border bg-white p-4 transition-opacity dark:bg-slate-900 ${
        pending
          ? pendingBorder
          : card.on
            ? "border-slate-200 dark:border-slate-800"
            : "border-dashed border-slate-300 dark:border-slate-700"
      } ${card.on ? "" : "opacity-70"}`}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold text-slate-900 dark:text-slate-100">
              {card.name}
            </span>
            <span className={pill}>{sourceLabel[card.source] ?? card.source}</span>
            {meta?.version ? (
              <span
                className={pill}
                title={meta.package ? `${meta.package} ${meta.version}` : undefined}
              >
                v{meta.version}
              </span>
            ) : null}
            {pending ? (
              <span className={pendingBadge}>
                {card.on ? "turns on when saved" : "turns off when saved"}
              </span>
            ) : !card.on ? (
              <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
                off
              </span>
            ) : null}
          </div>
          {card.description ? (
            <p
              className={`mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400 ${expanded ? "" : "line-clamp-2"}`}
              title={expanded ? undefined : card.description}
            >
              {card.description}
            </p>
          ) : null}
        </div>
        {onToggle && card.switchKey ? (
          <button
            type="button"
            role="switch"
            aria-checked={card.on}
            aria-label={`Enable ${card.switchKey}`}
            onClick={onToggle}
            className={`relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:outline-none dark:focus-visible:ring-offset-slate-900 ${
              card.on ? "bg-brand-600" : "bg-slate-300 dark:bg-slate-700"
            }`}
          >
            <span
              className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${
                card.on ? "left-[18px]" : "left-0.5"
              }`}
            />
          </button>
        ) : null}
      </div>

      {meta && (meta.model || meta.package || meta.key || (guarded && meta.gated !== undefined)) ? (
        <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500 dark:text-slate-400">
          {meta.key ? <Meta label="key" value={meta.key} /> : null}
          {meta.package ? <Meta label="package" value={meta.package} /> : null}
          {meta.model ? <Meta label="model" value={meta.model} /> : null}
          {guarded && meta.gated !== undefined ? (
            <div className="inline-flex items-center gap-1">
              <dt className="sr-only">gate</dt>
              {meta.gated ? (
                <dd className="inline-flex items-center gap-1">
                  <ShieldIcon size={12} className="text-ok-fg" />
                  approval gate on
                </dd>
              ) : (
                <dd className="inline-flex items-center gap-1 text-crit-fg">
                  <AlertIcon size={12} />
                  no approval gate
                </dd>
              )}
            </div>
          ) : null}
        </dl>
      ) : null}

      <div className="mt-3 flex flex-col gap-2.5 border-t border-slate-100 pt-3 dark:border-slate-800">
        {groups.map(({ guard, tools }) => (
          <div key={guard}>
            <p className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-slate-500 dark:text-slate-400">
              <span className={`h-1.5 w-1.5 rounded-full ${guardDot[guard]}`} />
              {guardLabel[guard]}
              <span className="text-slate-500 tabular-nums dark:text-slate-400">
                {tools.length}
              </span>
            </p>
            <ul className="flex flex-wrap gap-1" aria-label={`${card.name} ${guard} tools`}>
              {tools.map((t) => (
                <li
                  key={t.name}
                  title={`${guardTitle[t.guard]}${t.reason ? ` — ${t.reason}` : ""}`}
                  className={`rounded-md border px-1.5 py-0.5 font-mono text-[11px] ${guardChip[t.guard]}`}
                >
                  {t.name}
                </li>
              ))}
            </ul>
          </div>
        ))}
        {groups.length === 0 ? <p className="text-xs text-slate-500">No tools.</p> : null}
      </div>

      {card.description.length > 140 || onCompose ? (
        <div className="mt-3 flex items-center gap-3 text-xs">
          {card.description.length > 140 ? (
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => setExpanded((e) => !e)}
              className="font-medium text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
            >
              {expanded ? "Less" : "Full description"}
            </button>
          ) : null}
          {onCompose ? (
            <button
              type="button"
              onClick={onCompose}
              className="ml-auto font-medium text-brand-600 hover:underline dark:text-brand-400"
            >
              Compose from these tools
            </button>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="inline-flex min-w-0 items-center gap-1">
      <dt>{label}</dt>
      <dd className="truncate font-mono text-slate-700 dark:text-slate-200" title={value}>
        {value}
      </dd>
    </div>
  );
}
