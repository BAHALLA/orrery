import { useEffect, useId, useState } from "react";
import { ApiError } from "../api/client";
import { CheckIcon, CloseIcon, ShieldIcon } from "../components/icons";
import { toneChip, toneText } from "../components/severity";
import { describeApiError } from "../system/useSystem";
import type { StudioClient } from "./api";
import { sha256Hex } from "./digest";
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
/** A remote request's own cap (`MAX_REQUEST_CHARS`): an answer is sent to the remote as one. */
export const MAX_ANSWER = 8192;

/**
 * The decisions a run is waiting for, one card each (P0c).
 *
 * The text under review comes from `GET /studio/runs/{id}/approvals`, never
 * from the event stream (which carries only a redacted copy, and only when
 * the server captures content). It is shown exactly as the next node gets
 * it. Approve sends the SHA-256 this page computes of the text it shows
 * (checked against the server's), so the server refuses an approval of
 * anything the person did not read here; text the server could not send
 * whole can only be rejected. A remote's question (`kind: "input"`) is the
 * same card with an answer box: *Send answer* approves it with the answer,
 * which goes to the remote on its task; *Decline* sends the remote nothing.
 * The card goes
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
          <button
            type="button"
            className="underline"
            onClick={() => {
              setLoadError(null);
              setReload((n) => n + 1);
            }}
          >
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
  const [answer, setAnswer] = useState("");
  const asking = ask.kind === "input";
  const [sending, setSending] = useState<"approve" | "reject" | null>(null);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textId = useId();
  const hintId = useId();
  // The digest of the text shown, computed here: approving names it.
  const [checked, setChecked] = useState<{ text: string; digest: string | null } | null>(null);
  const text = pending?.text;
  useEffect(() => {
    if (text === undefined) return;
    let live = true;
    void sha256Hex(text).then((digest) => {
      if (live) setChecked({ text, digest });
    });
    return () => {
      live = false;
    };
  }, [text]);
  const expired = ask.expiresAt - now <= 0;
  const busy = sending !== null || sent;
  const ownDigest = checked && checked.text === text ? checked.digest : undefined;
  // Without Web Crypto (plain http) the server's digest stands: it is of the text it sent.
  const approveDigest =
    pending?.complete && ownDigest !== undefined
      ? ownDigest === null || ownDigest === pending.digest
        ? pending.digest
        : null
      : null;
  const blocked = !pending
    ? asking
      ? "You can answer once the question has been read."
      : "Approve is available once the text has been read."
    : !pending.complete
      ? `The ${asking ? "question" : "text"} is ${formatBytes(pending.size)}, too long to show in full: it can only be ${asking ? "declined" : "rejected"}.`
      : ownDigest === undefined
        ? "Checking the text…"
        : approveDigest === null
          ? `The ${asking ? "question" : "text"} shown does not match the server's digest: reload it before ${asking ? "answering" : "approving"}.`
          : expired
            ? asking
              ? "This question is expiring: the remote will be sent nothing."
              : "This approval is expiring: the run will end rejected."
            : asking && !answer.trim()
              ? "Write your answer to send it."
              : null;

  const decide = async (decision: "approve" | "reject") => {
    if (decision === "approve" && (blocked || !approveDigest)) return;
    setSending(decision);
    setError(null);
    const note = comment.trim();
    const extra = asking
      ? decision === "approve"
        ? { answer }
        : {}
      : note
        ? { comment: note }
        : {};
    try {
      await client.decide(
        runId,
        ask.approvalId,
        decision === "approve" && approveDigest
          ? { decision, digest: approveDigest, ...extra }
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
      aria-label={
        asking ? `Question from ${ask.remote ?? "a remote agent"}` : `Approval for ${ask.nodeId}`
      }
      className={`${panelCard} border-2 border-warn-border p-3`}
    >
      <div className="flex items-baseline gap-2">
        <p className="min-w-0 flex-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
          {asking
            ? `${ask.remote ?? "A remote agent"} asks:`
            : ask.prompt || "Approve this to continue?"}
        </p>
        <span
          className={`font-mono text-[11px] tabular-nums ${expired ? toneText.crit : toneText.warn}`}
          title={
            asking
              ? "Unanswered by then, the remote is sent nothing and the node goes on without it"
              : "Undecided by then, it expires and the run ends rejected"
          }
        >
          {timeLeft(ask.expiresAt, now)}
        </span>
      </div>
      <p className="mt-0.5 font-mono text-[11px] text-slate-500 dark:text-slate-400">
        {ask.nodeId} · {formatBytes(ask.size)}
      </p>

      {text !== undefined ? (
        <pre
          id={textId}
          tabIndex={0}
          aria-label={asking ? "The remote's question" : `Text under review for ${ask.nodeId}`}
          className="orrery-scroll mt-2 max-h-64 overflow-auto rounded-lg bg-slate-50 p-2 font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-slate-800 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:bg-slate-950 dark:text-slate-200"
        >
          {text}
        </pre>
      ) : (
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          Reading the text under review…
        </p>
      )}

      {asking ? (
        <>
          <p className="mt-2 text-[11px] text-slate-500 dark:text-slate-400">
            This question was written by an external agent, not by Orrery. Answer only what the task
            needs; never send secrets or credentials.
          </p>
          <label htmlFor={commentId} className={`${fieldLabel} mt-2`}>
            Your answer{" "}
            <span className="font-normal text-slate-500">(sent to the remote, nothing else)</span>
          </label>
          <textarea
            id={commentId}
            rows={3}
            maxLength={MAX_ANSWER}
            value={answer}
            disabled={busy}
            onChange={(e) => setAnswer(e.target.value)}
            className={`${input} mt-1 resize-y`}
          />
        </>
      ) : (
        <>
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
        </>
      )}

      {error ? (
        <p role="alert" className={`mt-2 rounded-lg border px-2 py-1.5 text-xs ${toneChip.crit}`}>
          {error}
        </p>
      ) : null}

      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          className={primaryButton}
          disabled={busy || blocked !== null}
          aria-describedby={blocked ? hintId : undefined}
          onClick={() => void decide("approve")}
        >
          <CheckIcon size={14} />
          {asking
            ? sending === "approve"
              ? "Sending…"
              : "Send answer"
            : sending === "approve"
              ? "Approving…"
              : "Approve"}
        </button>
        <button
          type="button"
          className={`${toolbarButton} border border-crit-border text-crit-fg hover:bg-crit-bg`}
          disabled={busy}
          onClick={() => void decide("reject")}
        >
          <CloseIcon size={14} />
          {asking
            ? sending === "reject"
              ? "Declining…"
              : "Decline"
            : sending === "reject"
              ? "Rejecting…"
              : "Reject"}
        </button>
        {sent ? (
          <span className="text-[11px] text-slate-500 dark:text-slate-400">
            Sent; the run resumes.
          </span>
        ) : null}
      </div>
      {blocked && !busy ? (
        <p id={hintId} className="mt-1.5 text-[11px] text-slate-500 dark:text-slate-400">
          {blocked}
        </p>
      ) : null}
    </article>
  );
}
