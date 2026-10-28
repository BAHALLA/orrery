import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

function read(key: string, fallback: boolean): boolean {
  try {
    const saved = localStorage.getItem(key);
    return saved === null ? fallback : saved === "1";
  } catch {
    return fallback;
  }
}

/**
 * A boolean layout preference that survives a reload (a folded rail, say).
 *
 * The setter is React's own, so it is stable and safe to call while rendering;
 * the value is written back from an effect. Storage can be blocked (private
 * mode, policy): the flag then lives for the page only, never an error — the
 * same rule as the theme choice.
 */
export function useStoredFlag(
  key: string,
  fallback = false,
): [boolean, Dispatch<SetStateAction<boolean>>] {
  const [value, setValue] = useState(() => read(key, fallback));
  useEffect(() => {
    try {
      localStorage.setItem(key, value ? "1" : "0");
    } catch {
      // Kept in memory only.
    }
  }, [key, value]);
  return [value, setValue];
}
