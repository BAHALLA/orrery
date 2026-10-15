import { useTheme, type ThemeChoice } from "../theme/theme";
import { MonitorIcon, MoonIcon, SunIcon } from "./icons";

const OPTIONS: ReadonlyArray<{ value: ThemeChoice; label: string; icon: typeof SunIcon }> = [
  { value: "light", label: "Light theme", icon: SunIcon },
  { value: "system", label: "Match system theme", icon: MonitorIcon },
  { value: "dark", label: "Dark theme", icon: MoonIcon },
];

/** Light / system / dark, as a three-way segmented control. */
export function ThemeToggle() {
  const { choice, setChoice } = useTheme();
  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className="flex rounded-lg bg-slate-100 p-0.5 dark:bg-slate-800"
    >
      {OPTIONS.map(({ value, label, icon: Icon }) => (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={choice === value}
          aria-label={label}
          title={label}
          onClick={() => setChoice(value)}
          className={`flex flex-1 items-center justify-center rounded-md py-1.5 transition focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none ${
            choice === value
              ? "bg-white text-slate-900 shadow-sm dark:bg-slate-700 dark:text-white"
              : "text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
          }`}
        >
          <Icon size={15} />
        </button>
      ))}
    </div>
  );
}
