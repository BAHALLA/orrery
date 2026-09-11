/**
 * The Orrery mark — three orbits, a sun, three planets. Drawn inline (not an
 * <img>) so the orbits follow the surrounding text colour via currentColor and
 * read correctly in both themes; the sun keeps its brand amber.
 *
 * Source of truth for the shape is docs/assets/brand/ (same geometry).
 */
export function OrreryMark({ className = "h-6 w-6" }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={className} aria-hidden="true" focusable="false" fill="none">
      <g stroke="currentColor" strokeWidth="2.4">
        <circle cx="32" cy="32" r="12" />
        <circle cx="32" cy="32" r="20" opacity=".7" />
        <circle cx="32" cy="32" r="28" opacity=".45" />
      </g>
      <circle cx="32" cy="32" r="5.5" fill="#F5B32B" />
      <g fill="currentColor">
        <circle cx="41.19" cy="24.29" r="2.8" />
        <circle cx="13.21" cy="38.84" r="3.3" />
        <circle cx="46" cy="56.25" r="3.6" />
      </g>
    </svg>
  );
}
