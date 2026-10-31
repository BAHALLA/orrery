import { config } from "../config";
import { branding } from "../theme/accent";
import { EnvironmentBadge } from "./EnvironmentBadge";
import { OrreryMark } from "./OrreryMark";

interface Props {
  onSignOut: () => void;
}

/**
 * Shown when the server verified the caller but refuses them this deployment
 * (`ORRERY_REQUIRED_ROLES`). Nothing else loads behind it: no conversations,
 * no status, no agents. Signing out lets someone switch account.
 */
export function AccessDenied({ onSignOut }: Props) {
  const url = config.accessDeniedUrl;
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4 dark:bg-slate-950">
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 text-center shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <div className="mb-1 flex items-center justify-center gap-2">
          <OrreryMark className="h-7 w-7 text-slate-900 dark:text-slate-100" />
          <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
            {branding.title}
          </h1>
          <EnvironmentBadge />
        </div>
        <h2 className="mt-4 text-base font-medium text-slate-900 dark:text-slate-100">
          You don&apos;t have access to this deployment
        </h2>
        <p className="mt-2 mb-5 text-sm text-slate-500 dark:text-slate-400">
          You are signed in, but your account doesn&apos;t have a role this deployment accepts.
        </p>
        {url ? (
          <a
            href={url}
            rel="noopener noreferrer"
            className="block w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 focus-visible:outline-none dark:focus-visible:ring-offset-slate-900"
          >
            Where to get access
          </a>
        ) : null}
        <button
          type="button"
          onClick={onSignOut}
          className="mt-3 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:outline-none dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
        >
          Sign out
        </button>
      </div>
    </div>
  );
}
