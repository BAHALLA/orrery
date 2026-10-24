import { useCallback, useRef, type KeyboardEvent } from "react";

/**
 * The WAI-ARIA tabs pattern, as the console's inspector does it: one tab stop
 * (the selected tab), arrow keys and Home/End move the selection and focus.
 * Spread `tabProps(t)` on each tab and put `onKeyDown` on the tablist.
 */
export function useTabs<T extends string>(tabs: readonly T[], value: T, onChange: (t: T) => void) {
  const refs = useRef<Partial<Record<T, HTMLButtonElement | null>>>({});

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLElement>) => {
      const index = tabs.indexOf(value);
      const next =
        e.key === "ArrowRight"
          ? tabs[(index + 1) % tabs.length]
          : e.key === "ArrowLeft"
            ? tabs[(index - 1 + tabs.length) % tabs.length]
            : e.key === "Home"
              ? tabs[0]
              : e.key === "End"
                ? tabs[tabs.length - 1]
                : undefined;
      if (next === undefined) return;
      e.preventDefault();
      onChange(next);
      refs.current[next]?.focus();
    },
    [tabs, value, onChange],
  );

  const tabProps = (t: T) => ({
    ref: (el: HTMLButtonElement | null) => {
      refs.current[t] = el;
    },
    role: "tab" as const,
    "aria-selected": value === t,
    tabIndex: value === t ? 0 : -1,
    onClick: () => onChange(t),
  });

  return { onKeyDown, tabProps };
}
