import { useEffect, useState, useSyncExternalStore, type RefObject } from "react";

/** What can take focus with Tab inside a container. */
const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => !el.closest("[inert]") && el.getAttribute("aria-hidden") !== "true",
  );
}

/**
 * Keep keyboard focus inside `ref` while `active`, and give it back after.
 *
 * For the drawers (sidebar below `lg`, inspector below `xl`), which cover the
 * page like a dialog: on activation focus moves to the first control inside;
 * Tab and Shift+Tab wrap at the ends instead of walking into the page behind
 * the scrim; on deactivation focus returns to whatever opened the drawer.
 * Escape is left to the caller, which already closes the topmost overlay.
 *
 * In-house rather than a focus-trap package: this is all the console needs,
 * and it is twenty lines.
 */
export function useFocusTrap(ref: RefObject<HTMLElement | null>, active: boolean): void {
  useEffect(() => {
    const root = ref.current;
    if (!active || !root) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!root.contains(document.activeElement)) (focusables(root)[0] ?? root).focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const items = focusables(root);
      if (items.length === 0) {
        e.preventDefault();
        root.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement;
      if (e.shiftKey && (current === first || !root.contains(current))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (current === last || !root.contains(current))) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      // Only hand focus back if it is still in the drawer (or nowhere): a
      // click that closed it on purpose elsewhere keeps its own focus.
      if (previous?.isConnected && (root.contains(document.activeElement) || isBody())) {
        previous.focus();
      }
    };
  }, [ref, active]);
}

function isBody(): boolean {
  return document.activeElement === null || document.activeElement === document.body;
}

/**
 * Whether a media query matches, live. Without `matchMedia` (jsdom, very old
 * browsers) it answers false — the narrow layout, where the drawers overlay.
 */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      if (typeof window.matchMedia !== "function") return () => undefined;
      const media = window.matchMedia(query);
      media.addEventListener("change", notify);
      return () => media.removeEventListener("change", notify);
    },
    () => typeof window.matchMedia === "function" && window.matchMedia(query).matches,
    () => false,
  );
}

/** Tailwind v4's default breakpoints, for layout decisions made in script. */
export const BREAKPOINT_LG = "(min-width: 64rem)";
export const BREAKPOINT_XL = "(min-width: 80rem)";

/**
 * A one-line announcement for screen readers when a turn starts and ends.
 *
 * The transcript used to be a live region, so every streamed token was read
 * out. Now it is marked busy while an answer is written, and this says the
 * two things worth hearing: that Orrery is working, and that the answer is
 * ready. A failed turn says nothing here — its error is a `role="alert"`.
 */
export function useTurnAnnouncement(busy: boolean, failed: boolean): string {
  const [prev, setPrev] = useState(busy);
  const [message, setMessage] = useState("");
  // Adjusted during render, React's pattern for state derived from a change.
  if (busy !== prev) {
    setPrev(busy);
    setMessage(busy ? "Orrery is working…" : failed ? "" : "Answer ready");
  }
  return message;
}
