import { useEffect, useId, useState } from "react";
import { ApiError } from "../api/client";
import { toneChip } from "../components/severity";
import { describeApiError } from "../system/useSystem";
import { SpecRefused, type StudioClient } from "./api";
import { Modal } from "./Dialogs";
import {
  describeChanges,
  fieldLabel,
  input,
  primaryButton,
  segmentTab,
  toolbarButton,
} from "./format";
import type { RegistryDocument, RegistryVersionSummary } from "./types";
import { useTabs } from "./useTabs";

const TABS = ["document", "history"] as const;
type RegistryTab = (typeof TABS)[number];

/** Longest note on a save (the server's `MAX_REGISTRY_NOTE`). */
const MAX_NOTE = 500;

const pretty = (doc: unknown) => JSON.stringify(doc, null, 2);

/**
 * The registry as written, and (with `ORRERY_STUDIO_REGISTRY_EDITING`) an
 * editor for it (P0c). A save names the version it was edited from, so two
 * admins editing at once get a conflict, never a silent overwrite; the
 * server checks an edit exactly as it checks the deployment's file, and
 * keeps every version with who saved it and why (the History tab).
 */
export function RegistryDialog({
  client,
  onSaved,
  onClose,
}: {
  client: StudioClient;
  /** A new version is live: read the registry again. */
  onSaved: () => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<RegistryTab>("document");
  const tabs = useTabs(TABS, tab, setTab);
  const panelId = useId();
  const [current, setCurrent] = useState<RegistryDocument | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    client
      .registryDocument(controller.signal)
      .then((doc) => {
        setCurrent(doc);
        setLoadError(null);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted)
          setLoadError(describeApiError(err, "The registry could not be read."));
      });
    return () => controller.abort();
  }, [client, reload]);

  const tabButton = (t: RegistryTab, label: string) => (
    <button
      type="button"
      {...tabs.tabProps(t)}
      id={`${panelId}-${t}`}
      aria-controls={panelId}
      disabled={t === "history" && !current?.editable}
      className={segmentTab(tab === t)}
    >
      {label}
    </button>
  );

  return (
    <Modal
      title="Registry"
      description="The MCP servers and remote agents a composition may reach. Secrets are named by their environment variable, never stored."
      onClose={onClose}
      wide
    >
      <div
        role="tablist"
        aria-label="Registry document or history"
        onKeyDown={tabs.onKeyDown}
        className="mb-3 inline-flex rounded-lg bg-slate-100 p-0.5 dark:bg-slate-800"
      >
        {tabButton("document", "Document")}
        {tabButton("history", "History")}
      </div>
      {loadError ? (
        <p role="alert" className={`rounded-lg border px-2 py-1.5 text-xs ${toneChip.crit}`}>
          {loadError}{" "}
          <button type="button" className="underline" onClick={() => setReload((n) => n + 1)}>
            Try again
          </button>
        </p>
      ) : null}
      <div role="tabpanel" id={panelId} aria-labelledby={`${panelId}-${tab}`}>
        {!current ? (
          loadError ? null : (
            <p className="text-xs text-slate-500 dark:text-slate-400">Reading the registry…</p>
          )
        ) : tab === "document" ? (
          <DocumentEditor
            key={current.version}
            client={client}
            current={current}
            onSaved={() => {
              setReload((n) => n + 1);
              onSaved();
            }}
            onReload={() => setReload((n) => n + 1)}
          />
        ) : (
          <History client={client} />
        )}
      </div>
    </Modal>
  );
}

function DocumentEditor({
  client,
  current,
  onSaved,
  onReload,
}: {
  client: StudioClient;
  current: RegistryDocument;
  onSaved: () => void;
  onReload: () => void;
}) {
  const textId = useId();
  const noteId = useId();
  const [text, setText] = useState(() => pretty(current.document));
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [conflict, setConflict] = useState(false);
  const [saved, setSaved] = useState<RegistryVersionSummary | null>(null);
  const origin =
    current.source === "file"
      ? "the deployment's file (version 0)"
      : `version ${current.version}, saved here`;

  if (!current.editable) {
    return (
      <>
        <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
          From {origin}. Editing is off on this server: it reads the file named by
          ORRERY_STUDIO_REGISTRY (set ORRERY_STUDIO_REGISTRY_EDITING=true to edit it here).
        </p>
        <pre
          tabIndex={0}
          aria-label="The registry document"
          className="orrery-scroll max-h-[55dvh] overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[12px] leading-relaxed text-slate-100"
        >
          {pretty(current.document)}
        </pre>
      </>
    );
  }

  const save = async () => {
    setProblems([]);
    setConflict(false);
    setSaved(null);
    let document: unknown;
    try {
      document = JSON.parse(text);
    } catch (err) {
      setProblems([`Not valid JSON: ${(err as Error).message}`]);
      return;
    }
    if (!document || typeof document !== "object" || Array.isArray(document)) {
      setProblems(["The registry is an object with `mcp` and `a2a` lists."]);
      return;
    }
    setSaving(true);
    try {
      const record = await client.saveRegistry({
        document: document as Record<string, unknown>,
        version: current.version,
        note: note.trim(),
      });
      setSaved(record);
      onSaved();
    } catch (err) {
      if (err instanceof SpecRefused) {
        setProblems(
          err.errors.map((e) => {
            const path = e.loc.filter((p) => p !== "document").join(" → ");
            return path ? `${path}: ${e.msg}` : e.msg;
          }),
        );
      } else if (err instanceof ApiError && err.status === 409) {
        setConflict(true);
      } else {
        setProblems([describeApiError(err, "The registry could not be saved.")]);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
        Editing {origin}. A save is checked exactly as the file is at startup (network allowlist,
        secrets, the acting opt-in), applies to runs started after it, and is kept with your name
        and note. An entry you change is fetched and pinned afresh.
      </p>
      <label htmlFor={textId} className={fieldLabel}>
        Registry (JSON)
      </label>
      <textarea
        id={textId}
        value={text}
        spellCheck={false}
        rows={18}
        disabled={saving}
        onChange={(e) => setText(e.target.value)}
        className={`${input} mt-1 resize-y font-mono text-[12px] leading-relaxed`}
      />
      <label htmlFor={noteId} className={`${fieldLabel} mt-2`}>
        Note <span className="font-normal text-slate-500">(why; kept in the history)</span>
      </label>
      <input
        id={noteId}
        value={note}
        maxLength={MAX_NOTE}
        disabled={saving}
        onChange={(e) => setNote(e.target.value)}
        className={`${input} mt-1`}
      />
      {problems.length ? (
        <ul role="alert" className={`mt-2 rounded-lg border px-3 py-2 text-xs ${toneChip.crit}`}>
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      ) : null}
      {conflict ? (
        <p role="alert" className={`mt-2 rounded-lg border px-2 py-1.5 text-xs ${toneChip.warn}`}>
          Someone saved the registry since you opened it. Copy your changes, then{" "}
          <button type="button" className="underline" onClick={onReload}>
            reload the latest
          </button>{" "}
          and apply them again.
        </p>
      ) : null}
      {saved ? (
        <p role="status" className={`mt-2 rounded-lg border px-2 py-1.5 text-xs ${toneChip.ok}`}>
          Saved as version {saved.version}: {describeChanges(saved.changes)}.
        </p>
      ) : null}
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          className={primaryButton}
          disabled={saving}
          onClick={() => void save()}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className={`${toolbarButton} border border-slate-200 dark:border-slate-700`}
          disabled={saving}
          onClick={() => {
            setText(pretty(current.document));
            setProblems([]);
          }}
        >
          Discard changes
        </button>
      </div>
    </>
  );
}

function History({ client }: { client: StudioClient }) {
  const [versions, setVersions] = useState<RegistryVersionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<{ version: number; text: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    client
      .registryHistory(controller.signal)
      .then(setVersions)
      .catch((err: unknown) => {
        if (!controller.signal.aborted)
          setError(describeApiError(err, "The history could not be read."));
      });
    return () => controller.abort();
  }, [client]);

  const show = async (version: number) => {
    try {
      const record = await client.registryVersion(version);
      setShown({ version, text: pretty(record.document) });
    } catch (err) {
      setError(describeApiError(err, `Version ${version} could not be read.`));
    }
  };

  if (error)
    return (
      <p role="alert" className={`rounded-lg border px-2 py-1.5 text-xs ${toneChip.crit}`}>
        {error}
      </p>
    );
  if (!versions)
    return <p className="text-xs text-slate-500 dark:text-slate-400">Reading the history…</p>;
  if (!versions.length)
    return (
      <p className="text-xs text-slate-500 dark:text-slate-400">
        No edits yet: the registry is the deployment's file.
      </p>
    );
  return (
    <>
      <ol className="flex flex-col gap-1.5" aria-label="Registry versions, newest first">
        {versions.map((v) => (
          <li
            key={v.version}
            className="rounded-lg border border-slate-200 px-3 py-2 text-xs dark:border-slate-700"
          >
            <div className="flex items-baseline gap-2">
              <span className="font-mono font-semibold text-slate-900 dark:text-slate-100">
                v{v.version}
              </span>
              <span className="min-w-0 flex-1 truncate text-slate-600 dark:text-slate-300">
                {v.actor} · {new Date(v.created_at * 1000).toLocaleString()}
              </span>
              <button
                type="button"
                className="text-brand-600 underline dark:text-brand-400"
                aria-label={`Show version ${v.version}`}
                onClick={() => void show(v.version)}
              >
                Show
              </button>
            </div>
            {v.note ? <p className="mt-0.5 text-slate-700 dark:text-slate-200">{v.note}</p> : null}
            <p className="mt-0.5 font-mono text-[11px] text-slate-500 dark:text-slate-400">
              {describeChanges(v.changes)}
            </p>
          </li>
        ))}
      </ol>
      {shown ? (
        <pre
          tabIndex={0}
          aria-label={`Registry version ${shown.version}`}
          className="orrery-scroll mt-3 max-h-[40dvh] overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[12px] leading-relaxed text-slate-100"
        >
          {shown.text}
        </pre>
      ) : null}
    </>
  );
}
