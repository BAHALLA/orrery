import { useCallback, useMemo, useRef, useState } from "react";
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
  /** Nothing was sent: nothing to save, or a save is already out. */
  | { ok: false; skipped: true }
  | { ok: false; issues: Issue[] }
  | { ok: false; conflict: number | null }
  | { ok: false; error: string };

export interface CompositionController {
  saved: Saved | null;
  /** The canvas differs from the saved version (or was never saved). */
  dirty: boolean;
  /** A save would change something: an edit, a first save, or restoring an older version. */
  canSave: boolean;
  saving: boolean;
  /**
   * Save the canvas: a new version of the saved composition, else a new one
   * (`asNew` forces that). A version older than the newest is saved over the
   * newest, as a restore. One save at a time; a save the canvas moved away
   * from meanwhile (another composition opened) is kept on the server but
   * does not relink the canvas.
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
  /** Set synchronously, so a held Ctrl+S never sends a second save before a render. */
  const inFlight = useRef(false);
  /** Bumped whenever the link changes: a save resolving after that does not relink. */
  const generation = useRef(0);
  const key = useMemo(() => keyOf(spec), [spec]);
  const dirty = saved ? saved.baseline !== key : spec.nodes.length > 0;
  const canSave =
    spec.nodes.length > 0 && !saving && (dirty || (!!saved && saved.version < saved.latest));

  const link = useCallback((next: Saved | null) => {
    generation.current += 1;
    setSaved(next);
  }, []);

  const adopt = useCallback(
    (record: CompositionRecord, onCanvas: CompositionSpec) =>
      link({
        id: record.id,
        version: record.version,
        latest: Math.max(record.version, ...record.versions),
        versions: record.versions,
        baseline: keyOf(onCanvas),
      }),
    [link],
  );

  const save = useCallback(
    async (current: CompositionSpec, asNew = false): Promise<SaveResult> => {
      if (inFlight.current || !current.nodes.length) return { ok: false, skipped: true };
      const restoring = !!saved && saved.version < saved.latest;
      if (!asNew && saved && saved.baseline === keyOf(current) && !restoring) {
        return { ok: false, skipped: true };
      }
      inFlight.current = true;
      setSaving(true);
      const startedAt = generation.current;
      try {
        const record =
          saved && !asNew
            ? await client.updateComposition(saved.id, current, saved.latest)
            : await client.createComposition(current);
        if (generation.current === startedAt) adopt(record, current);
        return { ok: true };
      } catch (err) {
        if (err instanceof SpecRefused) return { ok: false, issues: fromServer(err.errors) };
        if (err instanceof VersionConflict) return { ok: false, conflict: err.currentVersion };
        return { ok: false, error: describeApiError(err, "The composition could not be saved.") };
      } finally {
        inFlight.current = false;
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
    () => ({ saved, dirty, canSave, saving, save, adopt, restore: link, runSource }),
    [saved, dirty, canSave, saving, save, adopt, link, runSource],
  );
}
