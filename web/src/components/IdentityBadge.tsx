import type { MeResponse } from "../api/types";
import type { Identity } from "../auth/token";
import { LogOutIcon } from "./icons";

interface Props {
  identity: Identity | null;
  /** The server's own view, once loaded. Authoritative where it disagrees. */
  me: MeResponse | null;
  onSignOut: () => void;
  /** The folded rail: the avatar (its title says who and what) over sign-out. */
  compact?: boolean;
}

const autonomyTitle: Record<string, string> = {
  L2: "Autonomy L2 — read-only. Every mutating tool is blocked, whatever your role.",
  L3: "Autonomy L3 — mutating tools run; destructive ones are blocked.",
  L4: "Autonomy L4 — destructive tools run after an explicit human confirmation.",
};

const roleTitle: Record<Identity["role"], string> = {
  viewer: "Viewer — read-only. Mutating and destructive tools are blocked.",
  operator: "Operator — may run mutating tools; destructive tools need confirmation.",
  admin: "Admin — may run destructive tools after confirmation.",
};

const roleBadge: Record<Identity["role"], string> = {
  viewer: "bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
  operator: "bg-brand-100 text-brand-700 dark:bg-brand-950 dark:text-brand-300",
  admin: "bg-pink-100 text-pink-700 dark:bg-pink-950 dark:text-pink-300",
};

/**
 * Shows who the operator is acting as and their resolved role. Display only —
 * decoded client-side from the token; the server re-derives the authoritative
 * role via RBAC on every call.
 */
export function IdentityBadge({ identity, me, onSignOut, compact = false }: Props) {
  if (!identity) return null;
  // The server's resolution wins where the two differ — the token badge is the
  // browser's reading of a signature it cannot check.
  const role = me?.role ?? identity.role;
  const autonomy = me?.autonomy_level ?? null;
  const initial = identity.subject.trim().charAt(0).toUpperCase() || "?";
  const signOut = (
    <button
      type="button"
      onClick={onSignOut}
      aria-label="Sign out"
      title="Sign out"
      className="rounded-md p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-700 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:hover:bg-slate-800 dark:hover:text-slate-200"
    >
      <LogOutIcon />
    </button>
  );
  if (compact) {
    const who = `${identity.subject} · ${role}${autonomy ? ` · ${autonomy}` : ""}`;
    return (
      <div role="status" className="flex flex-col items-center gap-1.5">
        <span
          title={who}
          className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-brand-700 text-sm font-semibold text-white"
        >
          <span aria-hidden="true">{initial}</span>
          <span className="sr-only">{who}</span>
        </span>
        {signOut}
      </div>
    );
  }
  return (
    <div role="status" className="flex items-center gap-2.5">
      <span
        aria-hidden="true"
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-brand-700 text-sm font-semibold text-white"
      >
        {initial}
      </span>
      <div className="min-w-0 flex-1">
        <span
          className="block truncate text-sm font-medium text-slate-800 dark:text-slate-100"
          title={identity.subject}
        >
          {identity.subject}
        </span>
        <span className="mt-0.5 flex items-center gap-1">
          <span
            className={`rounded px-1.5 py-px text-[10px] font-semibold uppercase ${roleBadge[role]}`}
            title={roleTitle[role]}
          >
            {role}
          </span>
          {autonomy ? (
            <span
              className="rounded bg-slate-200 px-1.5 py-px text-[10px] font-semibold text-slate-600 dark:bg-slate-700 dark:text-slate-300"
              title={autonomyTitle[autonomy] ?? `Autonomy ${autonomy}`}
            >
              {autonomy}
            </span>
          ) : null}
        </span>
      </div>
      {signOut}
    </div>
  );
}
