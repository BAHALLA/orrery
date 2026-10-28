import { useEffect, useMemo, useState } from "react";
import { TrashIcon } from "../components/icons";
import { describeApiError } from "../system/useSystem";
import { VersionConflict, type StudioClient } from "./api";
import { Modal } from "./Dialogs";
import { ago, input, panelCard, subtleButton } from "./format";
import type { CompositionRecord, CompositionSummary } from "./types";

/**
 * The saved compositions (shared by every Studio admin): open the latest
 * version or an older one, or delete one. Deleting names the version that was
 * listed, so a composition someone saved again meanwhile is not deleted.
 */
export function CompositionsDialog({
  client,
  currentId,
  onOpen,
  onClose,
}: {
  client: StudioClient;
  /** The composition on the canvas, marked in the list. */
  currentId: string | null;
  onOpen: (record: CompositionRecord) => void;
  onClose: () => void;
}) {
  const [items, setItems] = useState<CompositionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [versions, setVersions] = useState<Record<string, number[]>>({});
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    client
      .compositions(controller.signal)
      .then((list) => {
        setItems([...list].sort((a, b) => b.updated_at - a.updated_at));
        setError(null);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted)
          setError(describeApiError(err, "The saved compositions could not be read."));
      });
    return () => controller.abort();
  }, [client, reload]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!items || !q) return items;
    return items.filter((c) =>
      [c.name, c.description, c.pattern, c.updated_by].some((f) => f.toLowerCase().includes(q)),
    );
  }, [items, query]);

  const open = async (id: string, version?: number) => {
    setBusyId(id);
    setError(null);
    try {
      onOpen(await client.composition(id, version));
    } catch (err) {
      setError(describeApiError(err, "The composition could not be opened."));
    } finally {
      setBusyId(null);
    }
  };

  const listVersions = async (id: string) => {
    setBusyId(id);
    try {
      const record = await client.composition(id);
      setVersions((v) => ({ ...v, [id]: [...record.versions].sort((a, b) => b - a) }));
    } catch (err) {
      setError(describeApiError(err, "The versions could not be read."));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (c: CompositionSummary) => {
    setBusyId(c.id);
    setError(null);
    try {
      await client.deleteComposition(c.id, c.version);
      setItems((list) => list?.filter((x) => x.id !== c.id) ?? null);
      setConfirmDelete(null);
    } catch (err) {
      setError(
        err instanceof VersionConflict
          ? `${c.name} was saved again since this list was read (now v${err.currentVersion ?? "?"}): nothing was deleted. The list is read again.`
          : describeApiError(err, "The composition could not be deleted."),
      );
      setConfirmDelete(null);
      if (err instanceof VersionConflict) setReload((n) => n + 1);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Modal
      title="Open a composition"
      description="Saved compositions are shared by every Studio admin, with each version kept."
      onClose={onClose}
      wide
    >
      {items && items.length > 4 ? (
        <input
          type="search"
          className={`${input} mb-3`}
          placeholder="Filter by name, pattern or author"
          aria-label="Filter compositions"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      ) : null}
      {error ? (
        <p role="alert" className="mb-3 rounded-lg bg-crit-bg px-3 py-2 text-xs text-crit-fg">
          {error}
        </p>
      ) : null}
      {shown === null ? (
        error ? null : (
          <p className="text-sm text-slate-500 dark:text-slate-400">Loading…</p>
        )
      ) : shown.length === 0 ? (
        <p className={`${panelCard} border-dashed p-4 text-sm text-slate-500 dark:text-slate-400`}>
          {items?.length
            ? "Nothing matches that filter."
            : "Nothing saved yet. Build a composition on the canvas and press Save: it is kept here, with every version, for every Studio admin."}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {shown.map((c) => {
            const busy = busyId === c.id;
            return (
              <li
                key={c.id}
                className={`${panelCard} p-3 ${c.id === currentId ? "border-brand-300 dark:border-brand-800" : ""}`}
              >
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="flex items-center gap-1.5">
                      <span className="truncate font-mono text-sm font-semibold text-slate-900 dark:text-slate-100">
                        {c.name}
                      </span>
                      <span className="rounded-full bg-slate-100 px-1.5 py-px text-[10px] font-semibold text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                        v{c.version}
                      </span>
                      {c.id === currentId ? (
                        <span className="text-[10px] font-semibold text-brand-700 dark:text-brand-300">
                          on the canvas
                        </span>
                      ) : null}
                    </p>
                    {c.description ? (
                      <p className="mt-0.5 line-clamp-2 text-xs text-slate-600 dark:text-slate-300">
                        {c.description}
                      </p>
                    ) : null}
                    <p className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
                      {c.pattern.replace(/_/g, " ")} · saved {ago(c.updated_at)} by{" "}
                      <span className="font-mono">{c.updated_by}</span>
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <button
                      type="button"
                      className={subtleButton}
                      disabled={busy}
                      onClick={() => void listVersions(c.id)}
                      aria-expanded={c.id in versions}
                    >
                      Versions
                    </button>
                    <button
                      type="button"
                      className={subtleButton}
                      disabled={busy}
                      aria-label={`Delete ${c.name}`}
                      onClick={() => setConfirmDelete(c.id)}
                    >
                      <TrashIcon size={13} />
                    </button>
                    <button
                      type="button"
                      className={`${subtleButton} bg-brand-50 text-brand-700 dark:bg-brand-950/60 dark:text-brand-200`}
                      disabled={busy}
                      onClick={() => void open(c.id)}
                    >
                      Open
                    </button>
                  </div>
                </div>
                {confirmDelete === c.id ? (
                  <div
                    role="alert"
                    className="mt-2 flex flex-wrap items-center gap-2 rounded-lg bg-crit-bg px-3 py-2 text-xs text-crit-fg"
                  >
                    <span className="flex-1">
                      Delete <span className="font-mono">{c.name}</span> and all its versions? Runs
                      already made keep their records.
                    </span>
                    <button
                      type="button"
                      className={subtleButton}
                      onClick={() => setConfirmDelete(null)}
                    >
                      Keep it
                    </button>
                    <button
                      type="button"
                      className="rounded-md bg-red-700 px-2 py-1 text-xs font-semibold text-white hover:bg-red-600 focus-visible:ring-2 focus-visible:ring-red-500 focus-visible:outline-none disabled:opacity-50"
                      disabled={busy}
                      onClick={() => void remove(c)}
                    >
                      Delete
                    </button>
                  </div>
                ) : null}
                {versions[c.id] ? (
                  <div className="mt-2 flex flex-wrap items-center gap-1">
                    <span className="text-[11px] text-slate-500 dark:text-slate-400">Open</span>
                    {versions[c.id].map((v) => (
                      <button
                        key={v}
                        type="button"
                        className={`${subtleButton} font-mono`}
                        disabled={busy}
                        onClick={() => void open(c.id, v)}
                        aria-label={`Open version ${v} of ${c.name}`}
                      >
                        v{v}
                      </button>
                    ))}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Modal>
  );
}
