import { CopyButton } from "../components/CopyButton";
import { AlertIcon, CheckIcon, LockIcon, OrbitSpinner } from "../components/icons";
import { toneChip, tonePill, type StatusTone } from "../components/severity";
import {
  formatBytes,
  formatMs,
  panelCard,
  sectionTitle,
  shortDigest,
  subtleButton,
} from "./format";
import type { A2aExchange, RunView } from "./runState";
import type { RegistryA2aEntry, StudioRegistry } from "./types";

/** A2A task states (a2a-sdk 1.x spells them `TASK_STATE_COMPLETED` on the wire, 0.x `completed`). */
function stateTone(state: string): StatusTone {
  const s = state
    .toLowerCase()
    .replace(/^task_state_/, "")
    .replace(/_/g, "-");
  if (s === "completed") return "ok";
  if (s === "failed" || s === "rejected") return "crit";
  if (s === "input-required" || s === "auth-required") return "warn";
  if (s === "canceled" || s === "cancelled") return "refused";
  return "running";
}

function stateLabel(state: string): string {
  return state
    .toLowerCase()
    .replace(/^task_state_/, "")
    .replace(/_/g, "-");
}

function pinnedBy(entry: RegistryA2aEntry): string {
  if (entry.card.pinned_by === "config") return "pinned from config (card_sha256)";
  if (entry.card.pinned_by) return `pinned on ${entry.card.pinned_by.replace(/_/g, " ")}`;
  return entry.card.resolved ? "fetched, not pinned" : "not fetched yet: pinned on first use";
}

/**
 * Remote A2A agents: what the registry trusts about each one, and what each
 * call in a run actually exchanged. The call view reads like the protocol:
 * a request leaves, the remote opens a task and moves it through states, a
 * reply comes back as parts.
 */
export function A2aInspector({
  registry,
  view,
  resolving,
  onResolve,
}: {
  registry: StudioRegistry | null;
  view: RunView | null;
  resolving: boolean;
  onResolve: () => void;
}) {
  const entries = registry?.a2a ?? [];
  const calls = view?.a2a ?? [];
  return (
    <div className="flex flex-col gap-4">
      <details
        className={`${panelCard} group p-3 text-xs leading-relaxed text-slate-600 dark:text-slate-300`}
      >
        <summary className="cursor-pointer font-medium text-slate-800 select-none dark:text-slate-100">
          How a remote call works
        </summary>
        <ol className="mt-2 flex list-decimal flex-col gap-1 pl-4">
          <li>
            Orrery reads the remote's <strong>agent card</strong> and checks its digest against the
            pin: a changed endpoint, auth scheme or skill list disables the remote until an admin
            re-pins it.
          </li>
          <li>
            The node's model calls a tool,{" "}
            <code className="font-mono">a2a_&lt;remote&gt;_&lt;skill&gt;</code>. Only the text it
            wrote leaves, as a <code className="font-mono">message/send</code>: never the session,
            earlier nodes or tool results, and never your token.
          </li>
          <li>
            The remote opens a <strong>task</strong> and moves it through states (submitted,
            working, completed…). A task that ends anywhere but completed is an error for the node.
          </li>
          <li>
            The reply comes back as <strong>parts</strong>. Text parts reach the model after the
            safety screen and PII redaction; other parts are dropped.
          </li>
        </ol>
      </details>

      <section aria-labelledby="studio-a2a-calls">
        <h3 id="studio-a2a-calls" className={sectionTitle}>
          Calls in this run {calls.length ? `(${calls.length})` : ""}
        </h3>
        {calls.length === 0 ? (
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            {view
              ? "This run made no remote calls."
              : "Run a composition with an a2a: tool to see its calls here."}
          </p>
        ) : (
          <ol className="mt-1.5 flex flex-col gap-2">
            {calls.map((x) => (
              <Exchange
                key={x.key}
                exchange={x}
                t0={view?.startedAt ?? 0}
                entry={entries.find((e) => e.id === x.remote)}
              />
            ))}
          </ol>
        )}
      </section>

      <section aria-labelledby="studio-a2a-remotes">
        <div className="flex items-center">
          <h3 id="studio-a2a-remotes" className={sectionTitle}>
            Remote agents {entries.length ? `(${entries.length})` : ""}
          </h3>
          {entries.length ? (
            <button
              type="button"
              className={`${subtleButton} ml-auto`}
              disabled={resolving}
              onClick={onResolve}
            >
              {resolving ? <OrbitSpinner size={12} /> : null}
              Fetch cards
            </button>
          ) : null}
        </div>
        {registry === null ? (
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Loading the registry…</p>
        ) : entries.length === 0 ? (
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            The registry lists no remote agents (<code className="font-mono">a2a:</code> in{" "}
            <code className="font-mono">ORRERY_STUDIO_REGISTRY</code>).
          </p>
        ) : (
          <ul className="mt-1.5 flex flex-col gap-2">
            {entries.map((e) => (
              <RemoteCard key={e.id} entry={e} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function RemoteCard({ entry }: { entry: RegistryA2aEntry }) {
  const { card } = entry;
  const cardSkills = new Map(card.skills.map((s) => [s.id, s]));
  return (
    <li className={`${panelCard} p-3`}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="font-mono text-sm font-semibold text-slate-900 dark:text-slate-100">
            {entry.id}
          </p>
          {entry.description ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">{entry.description}</p>
          ) : null}
        </div>
        <span
          className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ${entry.effect === "read_only" ? tonePill.ok : tonePill.guarded}`}
        >
          {entry.effect.replace("_", "-")}
        </span>
      </div>
      <p
        className="mt-1.5 truncate font-mono text-[11px] text-slate-500 dark:text-slate-400"
        title={entry.card_url}
      >
        {entry.card_url}
      </p>
      <div className="mt-1 flex flex-wrap gap-1 text-[10px]">
        {entry.private_network ? (
          <span className={`rounded px-1 py-px ${tonePill.neutral}`}>private network</span>
        ) : null}
        <span className={`inline-flex items-center gap-0.5 rounded px-1 py-px ${tonePill.neutral}`}>
          {entry.authenticated ? <LockIcon size={10} /> : null}
          {entry.authenticated ? "bearer token" : "no auth"}
        </span>
      </div>

      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-slate-500 dark:text-slate-400">Digest</dt>
        <dd className="flex min-w-0 items-center gap-1 font-mono text-slate-800 dark:text-slate-200">
          <span className="truncate" title={card.digest ?? undefined}>
            {shortDigest(card.digest)}
          </span>
          {card.digest ? <CopyButton value={card.digest} label="Copy the card digest" /> : null}
        </dd>
        <dt className="text-slate-500 dark:text-slate-400">Pin</dt>
        <dd className="text-slate-700 dark:text-slate-300">{pinnedBy(entry)}</dd>
        {card.fetched_at ? (
          <>
            <dt className="text-slate-500 dark:text-slate-400">Fetched</dt>
            <dd className="text-slate-700 dark:text-slate-300">
              {new Date(card.fetched_at * 1000).toLocaleTimeString()}
            </dd>
          </>
        ) : null}
      </dl>
      {card.error ? (
        <p
          className={`mt-2 flex items-start gap-1.5 rounded-md border px-2 py-1.5 text-xs ${toneChip.crit}`}
        >
          <AlertIcon size={13} className="mt-px shrink-0" />
          <span>
            Card not resolved: {card.error}. A run fetches it again, and refuses the call if it
            still fails.
          </span>
        </p>
      ) : null}

      <p className={`${sectionTitle} mt-2.5`}>Skills</p>
      <ul className="mt-1 flex flex-col gap-1">
        {entry.skills.map((id) => {
          const s = cardSkills.get(id);
          return (
            <li key={id} className="flex items-start gap-1.5 text-xs">
              <CheckIcon size={13} className="mt-px shrink-0 text-ok-fg" aria-hidden="true" />
              <span className="min-w-0">
                <span className="font-mono text-slate-800 dark:text-slate-200">{id}</span>
                {s?.name ? (
                  <span className="text-slate-500 dark:text-slate-400"> · {s.name}</span>
                ) : null}
                {s?.description ? (
                  <span className="block text-[11px] text-slate-500 dark:text-slate-400">
                    {s.description}
                  </span>
                ) : null}
              </span>
            </li>
          );
        })}
        {card.skills
          .filter((s) => !entry.skills.includes(s.id))
          .map((s) => (
            <li
              key={s.id}
              className="flex items-start gap-1.5 text-xs text-slate-500 dark:text-slate-400"
              title="On the card, not allowed by the registry entry"
            >
              <LockIcon size={12} className="mt-px shrink-0" />
              <span className="font-mono">{s.id}</span>
              <span>· on the card, not allowed here</span>
            </li>
          ))}
      </ul>
    </li>
  );
}

function Exchange({
  exchange: x,
  t0,
  entry,
}: {
  exchange: A2aExchange;
  t0: number;
  entry: RegistryA2aEntry | undefined;
}) {
  const out = x.outbound;
  const reply = x.inbound;
  const states = reply?.states?.length ? reply.states : reply?.task_state ? [reply.task_state] : [];
  const digest = out?.card_digest ?? reply?.card_digest ?? null;
  const pinned = entry?.card.digest ?? null;
  const refused = reply?.status === "refused" || out?.status === "refused";
  return (
    <li className={`${panelCard} overflow-hidden`}>
      <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50 px-3 py-1.5 text-[11px] dark:border-slate-800 dark:bg-slate-800/40">
        <span className="font-mono font-semibold text-slate-800 dark:text-slate-100">
          {x.nodeId}
        </span>
        <span className="text-slate-500 dark:text-slate-400">
          {out ? `+${(out.ts - t0).toFixed(1)}s` : ""}
        </span>
        <span className="font-mono font-semibold text-violet-700 dark:text-violet-300">
          {x.remote}.{x.skill}
        </span>
      </div>
      <ol
        className="flex flex-col gap-2 px-3 py-2.5 text-xs"
        aria-label={`Call from ${x.nodeId} to ${x.remote}`}
      >
        <li>
          <p className="text-[10px] font-semibold tracking-wide text-slate-500 uppercase dark:text-slate-400">
            1 · request
          </p>
          <Arrow
            direction="right"
            label={out?.method ?? "message/send"}
            detail={out?.parts?.map((p) => `${p.kind} ${formatBytes(p.size)}`).join(", ") ?? ""}
          />
        </li>
        <li>
          <p className="text-[10px] font-semibold tracking-wide text-slate-500 uppercase dark:text-slate-400">
            2 · task
          </p>
          {states.length ? (
            <ol className="mt-1 flex flex-wrap items-center gap-1" aria-label="Task states">
              {states.map((s, i) => (
                <li key={`${s}-${i}`} className="flex items-center gap-1">
                  {i ? (
                    <span aria-hidden="true" className="text-slate-400">
                      →
                    </span>
                  ) : null}
                  <span
                    className={`rounded-full border px-1.5 py-px font-mono text-[10px] ${toneChip[stateTone(s)]}`}
                  >
                    {stateLabel(s)}
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="mt-1 text-slate-500 dark:text-slate-400">
              {reply
                ? refused
                  ? "No task: the call was refused before anything was sent."
                  : "No task states reported."
                : "Waiting for the remote…"}
            </p>
          )}
          {reply?.task_id ? (
            <p className="mt-1 font-mono text-[10px] text-slate-500 dark:text-slate-400">
              task {reply.task_id.slice(0, 8)}
              {reply.context_id ? ` · context ${reply.context_id.slice(0, 8)}` : ""}
            </p>
          ) : null}
        </li>
        <li>
          <p className="text-[10px] font-semibold tracking-wide text-slate-500 uppercase dark:text-slate-400">
            3 · reply
          </p>
          {reply ? (
            <Arrow
              direction="left"
              label={reply.status ?? "reply"}
              detail={[
                reply.parts?.map((p) => `${p.kind} ${formatBytes(p.size)}`).join(", "),
                reply.elapsed_ms != null ? formatMs(reply.elapsed_ms) : "",
              ]
                .filter(Boolean)
                .join(" · ")}
              tone={reply.status === "success" ? "ok" : refused ? "refused" : "crit"}
            />
          ) : (
            <p className="mt-1 flex items-center gap-1.5 text-slate-500 dark:text-slate-400">
              <OrbitSpinner size={12} /> in flight
            </p>
          )}
        </li>
      </ol>
      {digest ? (
        <p className="border-t border-slate-100 px-3 py-1.5 font-mono text-[10px] text-slate-500 dark:border-slate-800 dark:text-slate-400">
          card {shortDigest(digest)}{" "}
          {pinned ? (
            pinned === digest ? (
              <span className="text-ok-fg">· matches the pin</span>
            ) : (
              <span className="text-crit-fg">· differs from the registry's pin</span>
            )
          ) : null}
        </p>
      ) : null}
      {reply?.content || out?.content ? (
        <details className="border-t border-slate-100 px-3 py-1.5 text-xs dark:border-slate-800">
          <summary className="cursor-pointer text-slate-600 dark:text-slate-300">
            Captured text (redacted)
          </summary>
          {out?.content ? (
            <pre className="mt-1 font-mono text-[11px] whitespace-pre-wrap">→ {out.content}</pre>
          ) : null}
          {reply?.content ? (
            <pre className="mt-1 font-mono text-[11px] whitespace-pre-wrap">← {reply.content}</pre>
          ) : null}
        </details>
      ) : null}
    </li>
  );
}

function Arrow({
  direction,
  label,
  detail,
  tone = "running",
}: {
  direction: "left" | "right";
  label: string;
  detail: string;
  tone?: StatusTone;
}) {
  const line = (
    <span aria-hidden="true" className="relative h-px flex-1 bg-slate-300 dark:bg-slate-600">
      <span
        className={`absolute top-1/2 h-0 w-0 -translate-y-1/2 border-y-4 border-y-transparent ${
          direction === "right"
            ? "-right-px border-l-[6px] border-l-slate-400"
            : "-left-px border-r-[6px] border-r-slate-400"
        }`}
      />
    </span>
  );
  return (
    <div className="mt-1 flex items-center gap-2">
      {direction === "left" ? line : null}
      <span
        className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[10px] ${toneChip[tone]}`}
      >
        {label}
      </span>
      {detail ? (
        <span className="shrink-0 font-mono text-[10px] text-slate-500 dark:text-slate-400">
          {detail}
        </span>
      ) : null}
      {direction === "right" ? line : null}
      <span className="sr-only">
        {direction === "right" ? "sent to the remote" : "returned to the node"}
      </span>
    </div>
  );
}
