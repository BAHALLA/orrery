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
import type { ApprovalKind, PendingApproval } from "./types";

const MAX_COMMENT = 2000;
/** A remote request's own cap (`MAX_REQUEST_CHARS`): an answer is sent to the remote as one. */
export const MAX_ANSWER = 8192;

/** What a card says, by what is being decided. */
interface Wording {
  /** The thing under review: "text", "question", "request". */
  noun: string;
  heading: (ask: ApprovalAsk) => string;
  label: (ask: ApprovalAsk) => string;
  textLabel: (ask: ApprovalAsk) => string;
  /** Shown under the text, or null. */
  notice: string | null;
  approve: [idle: string, busy: string];
  reject: [idle: string, busy: string];
  /** "Before approving it" and "it can only be rejected", in this kind's words. */
  approving: string;
  rejected: string;
  expiring: string;
  timerTitle: string;
}

const remoteName = (ask: ApprovalAsk) => ask.remote ?? "a remote agent";

export const WORDING: Record<ApprovalKind, Wording> = {
  approval: {
    noun: "text",
    heading: (ask) => ask.prompt || "Approve this to continue?",
    label: (ask) => `Approval for ${ask.nodeId}`,
    textLabel: (ask) => `Text under review for ${ask.nodeId}`,
    notice: null,
    approve: ["Approve", "Approving…"],
    reject: ["Reject", "Rejecting…"],
    approving: "approving",
    rejected: "rejected",
    expiring: "This approval is expiring: the run will end rejected.",
    timerTitle: "Undecided by then, it expires and the run ends rejected",
  },
  input: {
    noun: "question",
    heading: (ask) => `${ask.remote ?? "A remote agent"} asks:`,
    label: (ask) => `Question from ${remoteName(ask)}`,
    textLabel: () => "The remote's question",
    notice:
      "This question was written by an external agent, not by Orrery. Answer only what the " +
      "task needs; never send secrets or credentials.",
    approve: ["Send answer", "Sending…"],
    reject: ["Decline", "Declining…"],
    approving: "answering",
    rejected: "declined",
    expiring: "This question is expiring: the remote will be sent nothing.",
    timerTitle: "Unanswered by then, the remote is sent nothing and the node goes on without it",
  },
  action: {
    noun: "request",
    heading: (ask) => `${ask.nodeId} wants ${remoteName(ask)} to act on this request:`,
    label: (ask) => `Action for ${remoteName(ask)}`,
    textLabel: () => "The request that would be sent",
    notice:
      "This request was written by a model and goes to an external agent that changes " +
      "things. Confirm only what you mean to happen.",
    approve: ["Confirm and send", "Sending…"],
    reject: ["Don't send", "Refusing…"],
    approving: "confirming",
    rejected: "refused",
    expiring: "This request is expiring: it will not be sent.",
    timerTitle: "Unconfirmed by then, nothing is sent and the node goes on without it",
  },
};

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
  const words = WORDING[ask.kind];
  const [sending, setSending] = useState<"approve" | "reject" | null>(null);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textId = useId();
  const hintId = useId();
  const noticeId = useId();
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
    ? `You can decide once the ${words.noun} has been read.`
    : !pending.complete
      ? `The ${words.noun} is ${formatBytes(pending.size)}, too long to show in full: it can only be ${words.rejected}.`
      : ownDigest === undefined
        ? `Checking the ${words.noun}…`
        : approveDigest === null
          ? `The ${words.noun} shown does not match the server's digest: reload it before ${words.approving}.`
          : expired
            ? words.expiring
            : asking && !answer.trim()
              ? "Write your answer to send it."
              : null;

  const decide = async (decision: "approve" | "reject") => {
    if (decision === "approve" && (blocked || !approveDigest)) return;
    setSending(decision);
    setError(null);
    const note = comment.trim();
    const extra =
      ask.kind === "input"
        ? decision === "approve"
          ? { answer }
          : {}
        : ask.kind === "approval" && note
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
      aria-label={words.label(ask)}
      className={`${panelCard} border-2 border-warn-border p-3`}
    >
      <div className="flex items-baseline gap-2">
        <p className="min-w-0 flex-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
          {words.heading(ask)}
        </p>
        <span
          className={`font-mono text-[11px] tabular-nums ${expired ? toneText.crit : toneText.warn}`}
          title={words.timerTitle}
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
          aria-label={words.textLabel(ask)}
          className="orrery-scroll mt-2 max-h-64 overflow-auto rounded-lg bg-slate-50 p-2 font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-slate-800 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:bg-slate-950 dark:text-slate-200"
        >
          {text}
        </pre>
      ) : (
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          Reading the {words.noun} under review…
        </p>
      )}

      {words.notice ? (
        <p id={noticeId} className="mt-2 text-[11px] text-slate-500 dark:text-slate-400">
          {words.notice}
        </p>
      ) : null}
      {asking ? (
        <>
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
      ) : ask.kind === "approval" ? (
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
      ) : null}

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
          aria-describedby={
            [blocked ? hintId : null, words.notice ? noticeId : null].filter(Boolean).join(" ") ||
            undefined
          }
          onClick={() => void decide("approve")}
        >
          <CheckIcon size={14} />
          {words.approve[sending === "approve" ? 1 : 0]}
        </button>
        <button
          type="button"
          className={`${toolbarButton} border border-crit-border text-crit-fg hover:bg-crit-bg`}
          disabled={busy}
          onClick={() => void decide("reject")}
        >
          <CloseIcon size={14} />
          {words.reject[sending === "reject" ? 1 : 0]}
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
