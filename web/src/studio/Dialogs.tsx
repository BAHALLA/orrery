import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useFocusTrap } from "../components/a11y";
import { CopyButton } from "../components/CopyButton";
import { CloseIcon } from "../components/icons";
import { toneChip } from "../components/severity";
import { input, primaryButton, toolbarButton } from "./format";
import { DownloadIcon, UploadIcon } from "./icons";
import { TemplateGallery } from "./Library";
import type { Capabilities } from "./kinds";
import { parseSpec, specFileName, specJson } from "./specIo";
import type { Template } from "./templates";
import type { CompositionSpec } from "./types";
import { useTabs } from "./useTabs";

/** A modal dialog: focus moves in and stays, Escape or the scrim closes it, focus returns after. */
export function Modal({
  title,
  description,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useFocusTrap(ref, true);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center p-0 sm:items-center sm:p-4">
      <div
        aria-hidden="true"
        onClick={onClose}
        className="absolute inset-0 bg-slate-950/40 backdrop-blur-sm"
      />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`relative flex max-h-[90dvh] w-full flex-col rounded-t-2xl bg-white shadow-2xl ring-1 ring-slate-900/5 sm:rounded-2xl dark:bg-slate-900 dark:ring-white/10 ${
          wide ? "sm:max-w-3xl" : "sm:max-w-xl"
        }`}
      >
        <div className="flex items-start gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="font-semibold text-slate-900 dark:text-slate-100">
              {title}
            </h2>
            {description ? (
              <p className="text-xs text-slate-500 dark:text-slate-400">{description}</p>
            ) : null}
          </div>
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-800 focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none dark:hover:bg-slate-800 dark:hover:text-slate-100"
          >
            <CloseIcon />
          </button>
        </div>
        <div className="orrery-scroll min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  );
}

export function TemplatesDialog({
  templates,
  caps,
  onPick,
  onClose,
}: {
  templates: readonly Template[];
  caps: Capabilities;
  onPick: (t: Template) => void;
  onClose: () => void;
}) {
  return (
    <Modal
      title="Start from a pattern"
      description="Loading a template replaces the canvas (you can undo it). Patterns this server cannot run yet are marked with the phase that brings them."
      onClose={onClose}
      wide
    >
      <TemplateGallery templates={templates} caps={caps} onPick={onPick} />
    </Modal>
  );
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

const SPEC_TABS = ["export", "import"] as const;

/** The spec as JSON (copy, download), and loading one (paste or file). */
export function SpecDialog({
  spec,
  initialTab,
  onImport,
  onClose,
}: {
  spec: CompositionSpec;
  initialTab: "export" | "import";
  onImport: (spec: CompositionSpec, notes: string[]) => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState(initialTab);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const json = specJson(spec);
  const pasteId = useId();
  const fileId = useId();
  const panelId = useId();

  const load = (source: string) => {
    const result = parseSpec(source);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    onImport(result.spec, result.notes);
  };

  const tabs = useTabs(SPEC_TABS, tab, setTab);
  const tabButton = (t: "export" | "import", label: string) => (
    <button
      type="button"
      {...tabs.tabProps(t)}
      id={`${panelId}-${t}`}
      aria-controls={panelId}
      className={`rounded-md px-3 py-1 text-sm font-medium focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none ${
        tab === t
          ? "bg-white text-slate-900 shadow-sm dark:bg-slate-700 dark:text-white"
          : "text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-100"
      }`}
    >
      {label}
    </button>
  );

  return (
    <Modal
      title="Composition JSON"
      description="The spec is the whole composition: no positions, no code. The server validates it again on every run."
      onClose={onClose}
      wide
    >
      <div
        role="tablist"
        aria-label="Export or import"
        onKeyDown={tabs.onKeyDown}
        className="mb-3 inline-flex rounded-lg bg-slate-100 p-0.5 dark:bg-slate-800"
      >
        {tabButton("export", "Export")}
        {tabButton("import", "Import")}
      </div>
      {tab === "export" ? (
        <div role="tabpanel" id={panelId} aria-labelledby={`${panelId}-export`}>
          <pre className="orrery-scroll max-h-[50dvh] overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[12px] leading-relaxed text-slate-100">
            {json}
          </pre>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <CopyButton
              value={json}
              label="Copy the spec as JSON"
              className={`${toolbarButton} border border-slate-200 dark:border-slate-700`}
            />
            <button
              type="button"
              className={primaryButton}
              onClick={() => download(specFileName(spec), json)}
            >
              <DownloadIcon size={14} /> Download {specFileName(spec)}
            </button>
          </div>
        </div>
      ) : (
        <div
          role="tabpanel"
          id={panelId}
          aria-labelledby={`${panelId}-import`}
          className="flex flex-col gap-3"
        >
          <div>
            <label
              htmlFor={pasteId}
              className="block text-xs font-medium text-slate-700 dark:text-slate-300"
            >
              Paste a spec (or a whole <code className="font-mono">POST /studio/runs</code> body)
            </label>
            <textarea
              id={pasteId}
              rows={10}
              value={text}
              spellCheck={false}
              onChange={(e) => {
                setText(e.target.value);
                setError(null);
              }}
              className={`${input} mt-1 font-mono text-[12px]`}
              placeholder='{"spec_version": 1, "name": "…", "nodes": […], "edges": […]}'
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className={primaryButton}
              disabled={!text.trim()}
              onClick={() => load(text)}
            >
              Load onto the canvas
            </button>
            <label
              htmlFor={fileId}
              className={`${toolbarButton} cursor-pointer border border-slate-200 dark:border-slate-700`}
            >
              <UploadIcon size={14} /> Choose a file…
            </label>
            <input
              id={fileId}
              type="file"
              accept="application/json,.json"
              className="sr-only"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                void file.text().then((t) => {
                  setText(t);
                  load(t);
                });
                e.target.value = "";
              }}
            />
          </div>
          {error ? (
            <p role="alert" className={`rounded-lg border px-2.5 py-2 text-xs ${toneChip.crit}`}>
              {error}
            </p>
          ) : null}
        </div>
      )}
    </Modal>
  );
}
