import { useEffect, useId, useState } from "react";
import { ApiError } from "../api/client";
import { CheckIcon, CloseIcon, ShieldIcon } from "../components/icons";
import { toneChip, toneText } from "../components/severity";
import { describeApiError } from "../system/useSystem";
import type { StudioClient } from "./api";
import {
  fieldLabel,
  formatBytes,
  input,
  panelCard,
  primaryButton,
  timeLeft,
  toolbarButton,
} from "./format";
import type { ApprovalAsk } from "./runState";
import type { PendingApproval } from "./types";

const MAX_COMMENT = 2000;

/**
 * The decisions a run is waiting for, one card each (P0c).
 *
 * The text under review comes from `GET /studio/runs/{id}/approvals`, never
 * from the event stream (which carries it only when the server captures
 * content). Approve sends the digest of exactly that text, so the server
 * refuses an approval of anything the person did not read here. The card goes
 * away when the run's `approval.decided` event arrives, not when the POST
 * returns: the stream is the one source of what the run did.
 */
export function ApprovalCards({
  client,
  runId,
  asks,
  now,
}: {
  client: StudioClient;
  runId: string;
  asks: ApprovalAsk[];
  now: number;
}) {
  const [loaded, setLoaded] = useState<Record<string, PendingApproval>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const ids = asks.map((a) => a.approvalId).join(",");

  useEffect(() => {
    if (!ids) return;
    const controller = new AbortController();
    client
      .approvals(runId, controller.signal)
      .then(({ approvals }) => {
        setLoaded(Object.fromEntries(approvals.map((a) => [a.approval_id, a])));
        setLoadError(null);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setLoadError(describeApiError(err, "The text under review could not be read."));
      });
    return () => controller.abort();
  }, [client, runId, ids, reload]);

  if (!asks.length) return null;
  return (
    <section aria-labelledby="studio-approvals" className="flex flex-col gap-2">
      <h3 id="studio-approvals" className={`flex items-center gap-1.5 ${toneText.warn}`}>
        <ShieldIcon size={14} />
        <span className="text-[11px] font-semibold tracking-wider uppercase">
          Waiting for you ({asks.length})
        </span>
      </h3>
      {loadError ? (
        <p role="alert" className="text-xs text-crit-fg">
          {loadError}{" "}
          <button type="button" className="underline" onClick={() => setReload((n) => n + 1)}>
            Try again
          </button>
        </p>
      ) : null}
      {asks.map((ask) => (
        <ApprovalCard
          key={ask.approvalId}
          client={client}
          runId={runId}
          ask={ask}
          pending={loaded[ask.approvalId]}
          now={now}
          onStale={() => setReload((n) => n + 1)}
        />
      ))}
    </section>
  );
}

function ApprovalCard({
  client,
  runId,
  ask,
  pending,
  now,
  onStale,
}: {
  client: StudioClient;
  runId: string;
  ask: ApprovalAsk;
  /** From the approvals list: carries the text and the digest to approve with. */
  pending: PendingApproval | undefined;
  now: number;
  /** The server says this approval moved on: read the list again. */
  onStale: () => void;
}) {
  const commentId = useId();
  const [comment, setComment] = useState("");
  const [sending, setSending] = useState<"approve" | "reject" | null>(null);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const text = pending?.text ?? ask.content;
  const expired = ask.expiresAt - now <= 0;
  const busy = sending !== null || sent;

  const decide = async (decision: "approve" | "reject") => {
    // Approving names the digest of the text read here; without it, nothing to approve.
    if (decision === "approve" && !pending) return;
    setSending(decision);
    setError(null);
    const note = comment.trim();
    const extra = note ? { comment: note } : {};
    try {
      await client.decide(
        runId,
        ask.approvalId,
        decision === "approve" && pending
          ? { decision, digest: pending.digest, ...extra }
          : { decision: "reject", ...extra },
      );
      setSent(true);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 409)) {
        setError(
          err.status === 404
            ? "This approval is no longer pending: it was decided, or it expired."
            : err.message || "The server refused this decision.",
        );
        onStale();
      } else {
        setError(describeApiError(err, "The decision could not be sent."));
      }
    } finally {
      setSending(null);
    }
  };

  return (
    <article
      aria-label={`Approval for ${ask.nodeId}`}
      className={`${panelCard} border-2 border-warn-border p-3`}
    >
      <div className="flex items-baseline gap-2">
        <p className="min-w-0 flex-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
          {ask.prompt || "Approve this to continue?"}
        </p>
        <span
          className={`font-mono text-[11px] tabular-nums ${expired ? toneText.crit : toneText.warn}`}
          title="Undecided by then, it expires and the run ends rejected"
        >
          {timeLeft(ask.expiresAt, now)}
        </span>
      </div>
      <p className="mt-0.5 font-mono text-[11px] text-slate-500 dark:text-slate-400">
        {ask.nodeId} · {formatBytes(ask.size)}
      </p>

      {text !== null && text !== undefined ? (
        <pre className="orrery-scroll mt-2 max-h-64 overflow-auto rounded-lg bg-slate-50 p-2 font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-slate-800 dark:bg-slate-950 dark:text-slate-200">
          {text}
        </pre>
      ) : (
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          Reading the text under review…
        </p>
      )}

      <label htmlFor={commentId} className={`${fieldLabel} mt-2`}>
        Comment{" "}
        <span className="font-normal text-slate-500">(optional; a reject sends it back)</span>
      </label>
      <textarea
        id={commentId}
        rows={2}
        maxLength={MAX_COMMENT}
        value={comment}
        disabled={busy}
        onChange={(e) => setComment(e.target.value)}
        className={`${input} mt-1 resize-y`}
      />

      {error ? (
        <p role="alert" className={`mt-2 rounded-lg border px-2 py-1.5 text-xs ${toneChip.crit}`}>
          {error}
        </p>
      ) : null}

      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          className={primaryButton}
          disabled={busy || !pending}
          title={pending ? undefined : "Read the text before approving it"}
          onClick={() => void decide("approve")}
        >
          <CheckIcon size={14} />
          {sending === "approve" ? "Approving…" : "Approve"}
        </button>
        <button
          type="button"
          className={`${toolbarButton} border border-crit-border text-crit-fg hover:bg-crit-bg`}
          disabled={busy}
          onClick={() => void decide("reject")}
        >
          <CloseIcon size={14} />
          {sending === "reject" ? "Rejecting…" : "Reject"}
        </button>
        {sent ? (
          <span className="text-[11px] text-slate-500 dark:text-slate-400">
            Sent; the run resumes.
          </span>
        ) : null}
      </div>
    </article>
  );
}
