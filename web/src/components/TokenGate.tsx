import { useState } from "react";
import { OrreryMark } from "./OrreryMark";

interface Props {
  onSubmit: (token: string) => void;
  error?: string | null;
}

/**
 * First-run gate: the front door requires a JWT bearer token. Until AEP-013's
 * OAuth flow lands, the operator pastes a token (e.g. the dev JWT from
 * `make run-assistant-api`). It is stored locally and sent as `Authorization:
 * Bearer` on every request.
 */
export function TokenGate({ onSubmit, error }: Props) {
  const [value, setValue] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (trimmed) onSubmit(trimmed);
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4 dark:bg-slate-950">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
      >
        <div className="mb-1 flex items-center gap-2">
          <OrreryMark className="h-7 w-7 text-slate-900 dark:text-slate-100" />
          <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
            Orrery Console
          </h1>
        </div>
        <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
          Paste a bearer token to connect to the agent front door.
        </p>
        <label
          htmlFor="token"
          className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-200"
        >
          JWT bearer token
        </label>
        <textarea
          id="token"
          className="orrery-scroll w-full resize-none rounded-lg border border-slate-300 bg-slate-50 px-3 py-2 font-mono text-sm text-slate-900 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/30 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="eyJhbGciOi..."
          rows={4}
          autoFocus
          spellCheck={false}
        />
        {error ? (
          <p role="alert" className="mt-2 text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={!value.trim()}
          className="mt-4 w-full rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          Connect
        </button>
        <p className="mt-4 text-xs text-slate-400 dark:text-slate-500">
          The token is stored in your browser only and never leaves it except as an{" "}
          <code className="font-mono">Authorization</code> header to the API.
        </p>
      </form>
    </div>
  );
}
