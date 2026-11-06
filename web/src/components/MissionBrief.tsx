import type { Mission } from "../api/types";

interface Props {
  mission: Mission;
  headingLevel?: "h2" | "h3";
}

/** The server checks the link at startup; a browser would run `javascript:` as written. */
const isWebLink = (url: string) => /^https?:\/\//i.test(url);

/**
 * A lab's brief: the situation, the goal and where the tasks are. Plain text
 * from the operator's file, for the visitor to read; the model never sees it.
 */
export function MissionBrief({ mission, headingLevel: Heading = "h2" }: Props) {
  return (
    <section
      aria-label="Your mission"
      className="w-full rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm sm:p-5 dark:border-slate-800 dark:bg-slate-900"
    >
      <p className="text-[11px] font-semibold tracking-wider text-brand-700 uppercase dark:text-brand-300">
        Your mission
      </p>
      <Heading className="mt-1 text-lg font-semibold tracking-tight text-slate-900 dark:text-slate-50">
        {mission.title}
      </Heading>
      <p className="mt-2 text-sm whitespace-pre-line text-slate-600 dark:text-slate-300">
        {mission.situation}
      </p>
      <p className="mt-3 text-sm text-slate-900 dark:text-slate-100">
        <span className="font-medium">Your goal: </span>
        <span className="whitespace-pre-line">{mission.goal}</span>
      </p>
      {mission.tasks_url && isWebLink(mission.tasks_url) ? (
        <a
          href={mission.tasks_url}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-3 inline-block text-sm font-medium text-brand-700 underline underline-offset-2 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:text-brand-300"
        >
          {mission.tasks_label}
          <span aria-hidden="true"> ↗</span>
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
      ) : null}
    </section>
  );
}
