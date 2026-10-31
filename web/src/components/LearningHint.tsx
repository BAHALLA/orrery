import { useState } from "react";
import type { LearningInfo } from "../api/types";

/** What each level gives the learner, for the button's description. */
const NEXT_STEP: Record<number, string> = {
  0: "where to look",
  1: "which tool to run",
  2: "the answer",
};

interface Props {
  learning: LearningInfo;
  onTakeHint: () => Promise<string | null>;
}

/**
 * The learning sandbox's badge, hint level and Hint button (AEP-032 §3).
 *
 * The button is the only way to raise the level: the model has no tool for it.
 * Each press is one step and is recorded, so it never fires on its own.
 */
export function LearningHint({ learning, onTakeHint }: Props) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const atMax = learning.level >= learning.max_level;
  const next = NEXT_STEP[learning.level];

  async function take() {
    setPending(true);
    setError(null);
    setError(await onTakeHint());
    setPending(false);
  }

  return (
    <div className="flex items-center gap-2">
      <span className="hidden rounded-full bg-amber-100 px-2.5 py-1 text-xs font-medium text-amber-800 sm:inline-flex dark:bg-amber-950/60 dark:text-amber-200">
        Learning sandbox
      </span>
      <span className="text-xs text-slate-500 tabular-nums dark:text-slate-400">
        Hint {learning.level}/{learning.max_level}
      </span>
      {learning.can_take_hint ? (
        <button
          type="button"
          onClick={() => void take()}
          disabled={pending || atMax}
          aria-describedby="learning-hint-note"
          className="rounded-md border border-amber-300 px-2 py-1 text-xs font-medium text-amber-800 hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-amber-800 dark:text-amber-200 dark:hover:bg-amber-950/40"
        >
          {pending ? "Taking hint…" : "Take a hint"}
        </button>
      ) : null}
      <span id="learning-hint-note" className="sr-only">
        {atMax
          ? "No hints left: the answer is revealed."
          : `The next hint reveals ${next}. Each hint is recorded.`}
      </span>
      {error ? (
        <span role="alert" className="text-xs text-crit-fg">
          {error}
        </span>
      ) : null}
    </div>
  );
}
