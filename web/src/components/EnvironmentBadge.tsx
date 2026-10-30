import { branding, type Environment } from "../theme/accent";
import { tonePill, type StatusTone } from "./severity";

/** Production is the alarming one: approving there changes something real. */
const TONE: Record<Environment, StatusTone> = {
  production: "crit",
  staging: "warn",
  demo: "running",
  lab: "running",
};

const BANNER: Record<Exclude<Environment, "production">, string> = {
  staging: "Staging environment: changes here do not reach production.",
  demo: "Demo environment: seeded faults on throwaway infrastructure.",
  lab: "Lab environment: a practice system, safe to experiment on.",
};

/** The deployment's environment (`ORRERY_CONSOLE_ENVIRONMENT`); nothing when unset. */
export function EnvironmentBadge({
  environment = branding.environment,
}: {
  environment?: Environment | null;
}) {
  if (!environment) return null;
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] font-semibold tracking-wide whitespace-nowrap uppercase ${tonePill[TONE[environment]]}`}
    >
      {environment}
    </span>
  );
}

/** A one-line strip above the console for anything but production, which
 * gets the red badge instead: a banner everyone sees all day stops being read. */
export function EnvironmentBanner({
  environment = branding.environment,
}: {
  environment?: Environment | null;
}) {
  if (!environment || environment === "production") return null;
  return (
    <div
      role="note"
      className={`px-4 py-1 text-center text-xs font-medium ${tonePill[TONE[environment]]}`}
    >
      {BANNER[environment]}
    </div>
  );
}
