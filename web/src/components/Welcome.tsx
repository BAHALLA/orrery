import { ActivityIcon, GaugeIcon, RadarIcon, ShieldIcon } from "./icons";
import type { MeResponse, SpecialistInfo } from "../api/types";
import { joinLabels, specialistLabel, systemSpecialists } from "../specialists";
import { OrreryMark } from "./OrreryMark";

/** The part of `/me` that says what the caller can do here. */
export type Posture = Pick<MeResponse, "role" | "autonomy_level">;

interface Props {
  onSuggest: (prompt: string) => void;
  onSweep: () => void;
  /** The deployment's specialists; suggestions for the others are hidden. */
  specialists: readonly SpecialistInfo[];
  /** From `/me`; null until it answers. */
  me: Posture | null;
}

/** Starting points that exercise each specialist with a read-only question. */
const SUGGESTIONS: ReadonlyArray<{
  specialist: string;
  title: string;
  prompt: string;
  icon: typeof GaugeIcon;
}> = [
  {
    specialist: "kafka",
    title: "Kafka consumer lag",
    prompt: "Which Kafka consumer groups are lagging, and by how much?",
    icon: GaugeIcon,
  },
  {
    specialist: "k8s",
    title: "Pods in trouble",
    prompt: "Are any pods crash-looping or pending? Show recent warning events.",
    icon: ActivityIcon,
  },
  {
    specialist: "elasticsearch",
    title: "Elasticsearch health",
    prompt: "What is the Elasticsearch cluster health? Any unassigned shards?",
    icon: RadarIcon,
  },
  {
    specialist: "observability",
    title: "Firing alerts",
    prompt: "List the alerts firing right now and what they point to.",
    icon: ShieldIcon,
  },
];

/** "Ask about Kafka, Kubernetes, Docker or your alerts." from what is enabled. */
function askAbout(specialists: readonly SpecialistInfo[]): string {
  const systems = systemSpecialists(specialists);
  if (systems.length === 0) return "Ask about your systems.";
  // Observability answers questions about alerts: name what people ask about.
  const named = systems.filter((s) => s.key !== "observability").map(specialistLabel);
  if (systems.some((s) => s.key === "observability")) named.push("your alerts");
  return `Ask about ${joinLabels(named, "or")}.`;
}

/**
 * What this caller can do here, from their role and the autonomy level in force.
 * Display only: the server decides. Until `/me` answers (or if it says something
 * this console doesn't know) the line claims nothing either way.
 */
function postureLine(me: Posture | null): string {
  const neutral = "Orrery looks into them and explains what it finds.";
  if (!me) return neutral;
  const { role, autonomy_level: autonomy } = me;
  if (role === "viewer") {
    return autonomy === "L2"
      ? "You can investigate here. Neither your viewer role nor this read-only deployment (autonomy L2) can change a system."
      : "You can investigate and get recommendations. Your viewer role can't change systems, so Orrery refuses those requests.";
  }
  if (autonomy === "L2") {
    return "This deployment is read-only (autonomy L2): Orrery investigates and recommends, and won't change a system.";
  }
  if (role !== "operator" && role !== "admin") return neutral;
  switch (autonomy) {
    case "L3":
      return "Anything that would change a system waits for your approval first; destructive actions are off here (autonomy L3).";
    case "L4":
      return role === "admin"
        ? "Anything that would change a system waits for your approval first, destructive actions included (autonomy L4)."
        : "Anything that would change a system waits for your approval first; destructive actions need an admin (autonomy L4).";
    case null:
      return "Anything that would change a system waits for your approval first.";
    default:
      return neutral;
  }
}

/**
 * The empty conversation. One line of orientation and a few concrete starting
 * points — an operator usually arrives with a symptom, so the suggestions are
 * symptoms, each a read-only question a specialist can answer straight away.
 */
export function Welcome({ onSuggest, onSweep, specialists, me }: Props) {
  const enabled = new Set(specialists.map((s) => s.key));
  const suggestions = SUGGESTIONS.filter((s) => enabled.has(s.specialist));
  return (
    <div className="orrery-scroll relative flex flex-1 overflow-y-auto">
      <div className="m-auto flex w-full max-w-3xl flex-col items-center px-4 py-10 sm:px-6">
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-slate-900 text-white shadow-lg shadow-brand-900/20 dark:bg-slate-800">
          <OrreryMark className="h-9 w-9" />
        </div>
        <h2 className="text-center text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
          What would you like to check?
        </h2>
        {/* Room for the longest line, so the page doesn't move when /me answers. */}
        <p className="mt-2 min-h-20 max-w-lg text-center text-sm text-slate-500 sm:min-h-15 dark:text-slate-400">
          {askAbout(specialists)} {postureLine(me)}
        </p>

        <div className="mt-8 grid w-full gap-3 sm:grid-cols-2">
          {suggestions.map(({ title, prompt, icon: Icon }) => (
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
