import { useCallback, useMemo, useState } from "react";
import { describeApiError } from "../system/useSystem";
import { SpecRefused, VersionConflict, type StudioClient } from "./api";
import type { SavedRef } from "./useStudioRun";
import type { CompositionRecord, CompositionSpec } from "./types";
import { fromServer, type Issue } from "./validate";

/** The saved composition the canvas was opened from or last saved as. */
export interface Saved {
  id: string;
  /** The version on the canvas. */
  version: number;
  /** The newest version known: a save is checked against it. */
  latest: number;
  versions: number[];
  /** The canvas's spec as opened or saved, to tell whether it changed since. */
  baseline: string;
}

export type SaveResult =
  | { ok: true }
  | { ok: false; issues: Issue[] }
  | { ok: false; conflict: number | null }
  | { ok: false; error: string };

export interface CompositionController {
  saved: Saved | null;
  /** The canvas differs from the saved version (or was never saved). */
  dirty: boolean;
  saving: boolean;
  /**
   * Save the canvas: a new version of the saved composition, else a new one
   * (`asNew` forces that). A version older than the newest is saved over the
   * newest, as a restore.
   */
  save: (spec: CompositionSpec, asNew?: boolean) => Promise<SaveResult>;
  /** The canvas now shows *record* (just opened). */
  adopt: (record: CompositionRecord, spec: CompositionSpec) => void;
  /** Set or clear the saved composition (an undo puts the previous one back). */
  restore: (saved: Saved | null) => void;
  /** What a run of the canvas is a run of, when it is an unchanged saved version. */
  runSource: SavedRef | undefined;
}

const keyOf = (spec: CompositionSpec) => JSON.stringify(spec);

/**
 * The link between the canvas and a saved composition (`/studio/compositions`).
 *
 * The baseline is the *client's* spec at open or save time, not the server's
 * copy, which comes back with defaults filled in and so never compares equal.
 * Saves are optimistic: each names the version it was edited from, and a
 * newer one saved meanwhile by another admin is a conflict, never overwritten.
 */
export function useComposition(client: StudioClient, spec: CompositionSpec): CompositionController {
  const [saved, setSaved] = useState<Saved | null>(null);
  const [saving, setSaving] = useState(false);
  const key = useMemo(() => keyOf(spec), [spec]);
  const dirty = saved ? saved.baseline !== key : spec.nodes.length > 0;

  const adopt = useCallback((record: CompositionRecord, onCanvas: CompositionSpec) => {
    const latest = Math.max(record.version, ...record.versions);
    setSaved({
      id: record.id,
      version: record.version,
      latest,
      versions: record.versions,
      baseline: keyOf(onCanvas),
    });
  }, []);

  const save = useCallback(
    async (current: CompositionSpec, asNew = false): Promise<SaveResult> => {
      setSaving(true);
      try {
        const record =
          saved && !asNew
            ? await client.updateComposition(saved.id, current, saved.latest)
            : await client.createComposition(current);
        adopt(record, current);
        return { ok: true };
      } catch (err) {
        if (err instanceof SpecRefused) return { ok: false, issues: fromServer(err.errors) };
        if (err instanceof VersionConflict) return { ok: false, conflict: err.currentVersion };
        return { ok: false, error: describeApiError(err, "The composition could not be saved.") };
      } finally {
        setSaving(false);
      }
    },
    [client, saved, adopt],
  );

  const runSource = useMemo(
    () =>
      saved && saved.baseline === key
        ? { compositionId: saved.id, version: saved.version }
        : undefined,
    [saved, key],
  );

  return useMemo(
    () => ({ saved, dirty, saving, save, adopt, restore: setSaved, runSource }),
    [saved, dirty, saving, save, adopt, runSource],
  );
}
