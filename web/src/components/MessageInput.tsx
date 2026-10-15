import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { SendIcon, StopIcon } from "./icons";

interface Props {
  disabled: boolean;
  isSending: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}

/** Tallest the composer grows before it starts scrolling internally (px). */
const MAX_HEIGHT = 200;

/**
 * Composer. Enter sends; Shift+Enter inserts a newline; "/" from anywhere on
 * the page focuses it. Grows with the message up to {@link MAX_HEIGHT}.
 *
 * While a turn is in flight the Send button becomes **Stop**. Stopping only
 * stops *waiting*: the server finishes the turn, and the conversation shows the
 * answer when reopened.
 */
export function MessageInput({ disabled, isSending, onSend, onStop }: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Re-measure before paint so the box never flashes at the wrong height.
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto"; // reset first, or it can only ever grow
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  // "/" focuses the composer unless the user is already typing somewhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      if (target?.closest("input, textarea, [contenteditable='true']")) return;
      e.preventDefault();
      textareaRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const submit = () => {
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text);
    setValue("");
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="px-3 pb-3 sm:px-6 sm:pb-5">
      <form
        className="mx-auto flex w-full max-w-3xl items-end gap-2 rounded-2xl border border-slate-200 bg-white p-2 shadow-lg shadow-slate-900/5 focus-within:border-brand-400 focus-within:ring-4 focus-within:ring-brand-500/10 dark:border-slate-700 dark:bg-slate-900 dark:shadow-black/30"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          ref={textareaRef}
          className="orrery-scroll min-h-[2.75rem] flex-1 resize-none bg-transparent px-2.5 py-2.5 text-[15px] text-slate-900 placeholder:text-slate-400 focus:outline-none disabled:opacity-60 dark:text-slate-100 dark:placeholder:text-slate-500"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask Orrery about your systems…"
          rows={1}
          disabled={disabled}
          aria-label="Message"
          aria-describedby="composer-hint"
        />
        {isSending ? (
          <button
            type="button"
            onClick={onStop}
            aria-label="Stop"
            title="Stop waiting (the server finishes the turn)"
            className="flex h-10 items-center gap-1.5 rounded-xl border border-slate-300 px-3 text-sm font-medium text-slate-700 hover:bg-slate-100 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800"
          >
            <StopIcon size={16} />
            <span className="hidden sm:inline">Stop</span>
          </button>
        ) : (
          <button
            type="submit"
            disabled={disabled || !value.trim()}
            aria-label="Send"
            title="Send (Enter)"
            className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-600 text-white transition hover:bg-brand-500 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:outline-none disabled:bg-slate-200 disabled:text-slate-400 dark:focus-visible:ring-offset-slate-900 dark:disabled:bg-slate-800 dark:disabled:text-slate-600"
          >
            <SendIcon size={18} />
          </button>
        )}
      </form>
      <p
        id="composer-hint"
        className="mx-auto mt-1.5 hidden max-w-3xl px-2 text-[11px] text-slate-400 sm:block dark:text-slate-500"
      >
        <kbd className="font-sans">Enter</kbd> to send ·{" "}
        <kbd className="font-sans">Shift+Enter</kbd> for a new line ·{" "}
        <kbd className="font-sans">/</kbd> to focus
      </p>
    </div>
  );
}
