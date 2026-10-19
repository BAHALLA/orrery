import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { AttachmentsInfo } from "../api/types";
import { checkFiles, PICKER_ACCEPT, refsFor } from "../chat/attachments";
import { AttachmentChip } from "./AttachmentChip";
import { PaperclipIcon, SendIcon, StopIcon } from "./icons";

interface Props {
  disabled: boolean;
  isSending: boolean;
  /** The server's attachment limits; null hides attaching altogether. */
  attachments: AttachmentsInfo | null;
  onSend: (text: string, files: File[]) => void;
  onStop: () => void;
}

/** Tallest the composer grows before it starts scrolling internally (px). */
const MAX_HEIGHT = 200;

/**
 * Composer. Enter sends; Shift+Enter inserts a newline; "/" from anywhere on
 * the page focuses it. Grows with the message up to {@link MAX_HEIGHT}.
 *
 * Files (AEP-030) come in three ways — the paperclip, drag and drop onto the
 * composer, or pasting a screenshot — and are checked against the server's
 * limits before they are queued, so an obviously wrong file never uploads.
 *
 * While a turn is in flight the Send button becomes **Stop**. Stopping only
 * stops *waiting*: the server finishes the turn, and the conversation shows the
 * answer when reopened.
 */
export function MessageInput({ disabled, isSending, attachments, onSend, onStop }: Props) {
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [fileErrors, setFileErrors] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const pickerRef = useRef<HTMLInputElement>(null);

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

  const addFiles = (incoming: File[]) => {
    if (!attachments || !incoming.length) return;
    const result = checkFiles(files, incoming, attachments);
    setFiles(result.files);
    setFileErrors(result.errors);
  };

  const submit = () => {
    const text = value.trim();
    if ((!text && !files.length) || disabled) return;
    onSend(text, files);
    setValue("");
    setFiles([]);
    setFileErrors([]);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const canAttach = attachments !== null && !disabled;

  return (
    <div className="px-3 pb-3 sm:px-6 sm:pb-5">
      <form
        className={`mx-auto flex w-full max-w-3xl flex-col rounded-2xl border bg-white p-2 shadow-lg shadow-slate-900/5 focus-within:border-brand-400 focus-within:ring-4 focus-within:ring-brand-500/10 dark:bg-slate-900 dark:shadow-black/30 ${
          dragging
            ? "border-brand-400 ring-4 ring-brand-500/20"
            : "border-slate-200 dark:border-slate-700"
        }`}
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        onDragOver={(e) => {
          if (!canAttach || !e.dataTransfer.types.includes("Files")) return;
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
        }}
        onDrop={(e) => {
          if (!canAttach) return;
          e.preventDefault();
          setDragging(false);
          addFiles(Array.from(e.dataTransfer.files));
        }}
      >
        {files.length ? (
          <ul className="flex flex-wrap gap-1.5 px-1 pt-1 pb-1.5" aria-label="Attachments">
            {refsFor(files).map((ref, i) => (
              <li key={`${ref.name}-${i}`} className="min-w-0">
                <AttachmentChip
                  attachment={ref}
                  tone="composer"
                  onRemove={() => setFiles((list) => list.filter((_, j) => j !== i))}
                />
              </li>
            ))}
          </ul>
        ) : null}
        <div className="flex items-end gap-2">
          {attachments ? (
            <>
              <input
                ref={pickerRef}
                type="file"
                multiple
                hidden
                accept={[...attachments.image_types, PICKER_ACCEPT].join(",")}
                onChange={(e) => {
                  addFiles(Array.from(e.target.files ?? []));
                  e.target.value = ""; // so picking the same file again still fires
                }}
              />
              <button
                type="button"
                onClick={() => pickerRef.current?.click()}
                disabled={!canAttach}
                aria-label="Attach files"
                title={`Attach ${attachments.images ? "screenshots or " : ""}text files (or drop / paste them). Files are used for this message only and never stored; the reply about them is kept like any reply.`}
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-slate-500 hover:bg-slate-100 hover:text-slate-800 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none disabled:opacity-40 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100"
              >
                <PaperclipIcon size={18} />
              </button>
            </>
          ) : null}
          <textarea
            ref={textareaRef}
            className="orrery-scroll min-h-[2.75rem] flex-1 resize-none bg-transparent px-2.5 py-2.5 text-[15px] text-slate-900 placeholder:text-slate-400 focus:outline-none disabled:opacity-60 dark:text-slate-100 dark:placeholder:text-slate-500"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={(e) => {
              // A pasted screenshot arrives as a file; plain text pastes as usual.
              const pasted = Array.from(e.clipboardData.files);
              if (!canAttach || !pasted.length) return;
              e.preventDefault();
              addFiles(pasted);
            }}
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
              disabled={disabled || (!value.trim() && !files.length)}
              aria-label="Send"
              title="Send (Enter)"
              className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-600 text-white transition hover:bg-brand-500 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:outline-none disabled:bg-slate-200 disabled:text-slate-400 dark:focus-visible:ring-offset-slate-900 dark:disabled:bg-slate-800 dark:disabled:text-slate-600"
            >
              <SendIcon size={18} />
            </button>
          )}
        </div>
      </form>
      {fileErrors.length ? (
        <ul
          role="alert"
          className="mx-auto mt-1.5 max-w-3xl px-2 text-xs text-red-600 dark:text-red-400"
        >
          {fileErrors.map((err) => (
            <li key={err}>{err}</li>
          ))}
        </ul>
      ) : null}
      <p
        id="composer-hint"
        className="mx-auto mt-1.5 hidden max-w-3xl px-2 text-[11px] text-slate-400 sm:block dark:text-slate-500"
      >
        <kbd className="font-sans">Enter</kbd> to send ·{" "}
        <kbd className="font-sans">Shift+Enter</kbd> for a new line ·{" "}
        <kbd className="font-sans">/</kbd> to focus
        {attachments ? (
          <> · drop or paste {attachments.images ? "screenshots and " : ""}text files</>
        ) : null}
      </p>
    </div>
  );
}
