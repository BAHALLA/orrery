/**
 * Deployment branding (`ORRERY_CONSOLE_TITLE` / `_ENVIRONMENT` / `_ACCENT`),
 * so a production console and a demo lab are told apart at a glance.
 *
 * The server validates every value at startup; the checks here only keep an
 * old server or a hand-edited config from breaking the page.
 */

export type Environment = "production" | "staging" | "demo" | "lab";

const ENVIRONMENTS: readonly Environment[] = ["production", "staging", "demo", "lab"];

/** Hue of each preset; `slate` also drops the chroma to a near-grey. Must match
 * `ACCENTS` in `core/orrery_core/serving/console_config.py`. */
export const ACCENT_HUES: Record<string, number> = {
  indigo: 275,
  blue: 255,
  teal: 190,
  emerald: 160,
  amber: 75,
  orange: 50,
  red: 25,
  rose: 10,
  violet: 300,
  slate: 255,
};

/** Lightness and chroma of the `--color-brand-*` scale in `styles/index.css`. */
const SCALE: readonly [step: number, lightness: number, chroma: number][] = [
  [50, 0.97, 0.02],
  [100, 0.93, 0.04],
  [200, 0.87, 0.08],
  [300, 0.78, 0.12],
  [400, 0.68, 0.16],
  [500, 0.58, 0.2],
  [600, 0.51, 0.21],
  [700, 0.45, 0.19],
  [800, 0.38, 0.15],
  [900, 0.32, 0.11],
  [950, 0.23, 0.08],
];

const DEFAULT_TITLE = "Orrery Console";

export interface Branding {
  title: string;
  environment: Environment | null;
  accent: string | null;
}

export function resolveBranding(raw: OrreryRuntimeConfig["branding"]): Branding {
  const title = raw?.title?.trim();
  const environment = ENVIRONMENTS.find((e) => e === raw?.environment) ?? null;
  const accent = raw?.accent && raw.accent in ACCENT_HUES ? raw.accent : null;
  return { title: title || DEFAULT_TITLE, environment, accent };
}

export const branding: Branding = resolveBranding(
  typeof window !== "undefined" ? window.__ORRERY_CONSOLE_CONFIG__?.branding : undefined,
);

/** Repaint the brand scale in the chosen hue. Utilities read the variables, so
 * every `brand-*` class follows; the default leaves the stylesheet alone. */
export function applyAccent(accent: string | null, root: HTMLElement = document.documentElement) {
  if (!accent || accent === "indigo") return;
  const hue = ACCENT_HUES[accent];
  const chromaScale = accent === "slate" ? 0.15 : 1;
  for (const [step, lightness, chroma] of SCALE) {
    const c = +(chroma * chromaScale).toFixed(3);
    root.style.setProperty(`--color-brand-${step}`, `oklch(${lightness} ${c} ${hue})`);
  }
}

/** "PRODUCTION · Orrery Console": the environment first, so a pinned tab shows it. */
export function brandedTitle(b: Branding = branding): string {
  return b.environment ? `${b.environment.toUpperCase()} · ${b.title}` : b.title;
}
