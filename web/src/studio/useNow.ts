import { useEffect, useState } from "react";

/** How often the clock ticks while on, ms. */
export const TICK_MS = 250;

/**
 * Unix seconds, ticking while `on` (a run is live and the Studio is on
 * screen), for live elapsed times. Off, it neither ticks nor re-renders.
 */
export function useNow(on: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!on) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), TICK_MS);
    return () => clearInterval(timer);
  }, [on]);
  return now;
}
