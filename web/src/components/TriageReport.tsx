import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ActivityEntry, TriageResponse } from "../api/types";
import { severityAccent, severityBadge, tonePill } from "./severity";
import { specialistStatuses } from "./systemStatus";

interface Props {
  triage: TriageResponse;
  /** Tool calls for this session — the source of the per-system chips. */
  activity: ActivityEntry[];
}

const chipClass: Record<string, string> = {
  ok: tonePill.ok,
  partial: tonePill.warn,
  failed: tonePill.crit,
};

const chipIcon: Record<string, string> = { ok: "●", partial: "◐", failed: "✕" };

/**
 * The session's latest triage verdict: a severity header with the full report
 * rendered as prose. The verdict comes from `record_triage_verdict` (session
 * state), not from parsing the chat transcript, so the badge always reflects
 * the recorded machine-readable severity.
 */
export function TriageReport({ triage, activity }: Props) {
  const specialists = specialistStatuses(activity);
  if (!triage.severity) {
    return (
      <p className="p-4 text-sm text-slate-500 dark:text-slate-400">
        No triage verdict yet. Click <span className="font-medium">Run triage</span> to sweep every
        system.
      </p>
    );
  }

  return (
    <div className={`border-l-4 ${severityAccent[triage.severity]}`}>
      <div className="flex items-center gap-2 px-4 py-3">
        <span
          className={`rounded-full px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide ${severityBadge[triage.severity]}`}
        >
          {triage.severity}
        </span>
        <span className="text-sm font-medium text-slate-700 dark:text-slate-200">
          Latest triage verdict
        </span>
      </div>
      {specialists.length > 0 ? (
        <div className="flex flex-wrap gap-1.5 px-4 pb-3">
          {specialists.map((s) => (
            <span
              key={s.key}
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${chipClass[s.state]}`}
              title={
                s.failures === 0
                  ? `${s.label}: ${s.calls} call(s), all succeeded`
                  : `${s.label}: ${s.failures} of ${s.calls} call(s) failed`
              }
            >
              <span aria-hidden="true">{chipIcon[s.state]}</span>
              {s.label}
            </span>
          ))}
        </div>
      ) : null}
      {triage.report ? (
        <div className="prose prose-sm dark:prose-invert max-w-none px-4 pb-4">
          <Markdown remarkPlugins={[remarkGfm]}>{triage.report}</Markdown>
        </div>
      ) : null}
    </div>
  );
}
