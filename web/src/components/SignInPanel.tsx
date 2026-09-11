import { OrreryMark } from "./OrreryMark";

interface Props {
  onSignIn: () => void;
  error?: string | null;
  /** True while an existing session is being restored. */
  isLoading?: boolean;
}

/**
 * SSO sign-in surface, shown when `VITE_OIDC_ISSUER` is configured.
 *
 * Deliberately offers one action. Everything else — which provider, which
 * scopes, where to return — is deployment configuration, not a choice to put
 * in front of an operator who is usually here because something is on fire.
 */
export function SignInPanel({ onSignIn, error, isLoading = false }: Props) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4 dark:bg-slate-950">
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 text-center shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <div className="mb-1 flex items-center justify-center gap-2">
          <OrreryMark className="h-7 w-7 text-slate-900 dark:text-slate-100" />
          <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
            Orrery Console
          </h1>
        </div>

        {isLoading ? (
          <p className="py-6 text-sm text-slate-500 dark:text-slate-400" role="status">
            Restoring your session…
          </p>
        ) : (
          <>
            <p className="mb-5 text-sm text-slate-500 dark:text-slate-400">
              Sign in to reach your Kafka, Kubernetes, Elasticsearch, and Docker estate.
            </p>
            <button
              type="button"
              onClick={onSignIn}
              className="w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 focus-visible:outline-none dark:focus-visible:ring-offset-slate-900"
            >
              Sign in with SSO
            </button>
          </>
        )}

        {error ? (
          <p
            role="alert"
            className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/50 dark:text-red-300"
          >
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}
