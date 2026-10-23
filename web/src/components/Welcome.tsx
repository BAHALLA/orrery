import { ActivityIcon, GaugeIcon, RadarIcon, ShieldIcon } from "./icons";
import { OrreryMark } from "./OrreryMark";

interface Props {
  onSuggest: (prompt: string) => void;
  onSweep: () => void;
}

/** Starting points that exercise each specialist with a read-only question. */
const SUGGESTIONS: ReadonlyArray<{ title: string; prompt: string; icon: typeof GaugeIcon }> = [
  {
    title: "Kafka consumer lag",
    prompt: "Which Kafka consumer groups are lagging, and by how much?",
    icon: GaugeIcon,
  },
  {
    title: "Pods in trouble",
    prompt: "Are any pods crash-looping or pending? Show recent warning events.",
    icon: ActivityIcon,
  },
  {
    title: "Elasticsearch health",
    prompt: "What is the Elasticsearch cluster health? Any unassigned shards?",
    icon: RadarIcon,
  },
  {
    title: "Firing alerts",
    prompt: "List the alerts firing right now and what they point to.",
    icon: ShieldIcon,
  },
];

/**
 * The empty conversation. One line of orientation and a few concrete starting
 * points — an operator usually arrives with a symptom, so the suggestions are
 * symptoms, each a read-only question a specialist can answer straight away.
 */
export function Welcome({ onSuggest, onSweep }: Props) {
  return (
    <div className="orrery-scroll flex flex-1 overflow-y-auto">
      <div className="m-auto flex w-full max-w-3xl flex-col items-center px-4 py-10 sm:px-6">
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-slate-900 text-white shadow-lg shadow-brand-900/20 dark:bg-slate-800">
          <OrreryMark className="h-9 w-9" />
        </div>
        <h2 className="text-center text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
          What's happening in your estate?
        </h2>
        <p className="mt-2 max-w-lg text-center text-sm text-slate-500 dark:text-slate-400">
          Ask about Kafka, Kubernetes, Elasticsearch, Docker or your alerts. Anything that would
          change a system waits for your approval first.
        </p>

        <div className="mt-8 grid w-full gap-3 sm:grid-cols-2">
          {SUGGESTIONS.map(({ title, prompt, icon: Icon }) => (
            <button
              key={title}
              type="button"
              onClick={() => onSuggest(prompt)}
              className="group flex items-start gap-3 rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:border-brand-300 hover:shadow-md focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:border-slate-800 dark:bg-slate-900 dark:hover:border-brand-700"
            >
              <span className="mt-0.5 rounded-lg bg-brand-50 p-2 text-brand-600 dark:bg-brand-950 dark:text-brand-300">
                <Icon size={18} />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-slate-900 dark:text-slate-100">
                  {title}
                </span>
                <span className="mt-0.5 block text-xs text-slate-500 dark:text-slate-400">
                  {prompt}
                </span>
              </span>
            </button>
          ))}
        </div>

        <button
          type="button"
          onClick={onSweep}
          className="mt-4 flex w-full items-center justify-between gap-3 rounded-xl border border-dashed border-slate-300 px-4 py-3 text-left text-sm text-slate-600 transition hover:border-brand-400 hover:text-slate-900 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:border-slate-700 dark:text-slate-300 dark:hover:text-white"
        >
          <span>
            <span className="font-medium">Full health sweep</span>
            <span className="text-slate-500 dark:text-slate-400">
              {" "}
              — every specialist in parallel, one verdict
            </span>
          </span>
          <span aria-hidden="true">→</span>
        </button>
      </div>
    </div>
  );
}
