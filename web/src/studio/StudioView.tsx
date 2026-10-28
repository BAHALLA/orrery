import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { ApiError } from "../api/client";
import { BREAKPOINT_LG, useMediaQuery } from "../components/a11y";
import { AlertIcon, CheckIcon, CloseIcon, LockIcon, StudioIcon } from "../components/icons";
import { toneChip, tonePill } from "../components/severity";
import { describeApiError } from "../system/useSystem";
import { A2aInspector } from "./A2aInspector";
import { StudioClient } from "./api";
import { Canvas } from "./Canvas";
import { CompositionsDialog } from "./CompositionsDialog";
import { SpecDialog, TemplatesDialog } from "./Dialogs";
import { RegistryDialog } from "./RegistryDialog";
import { panelCard, primaryButton, runLabel, sectionTitle, toolbarButton } from "./format";
import {
  addNode,
  connect,
  relabel,
  graphToSpec,
  relayout,
  removeEdges,
  reorient,
  removeNodes,
  specToGraph,
  uniqueId,
  updateNode,
  type Direction,
  type StudioGraph,
} from "./graph";
import { HistoryPanel } from "./HistoryPanel";
import {
  DownloadIcon,
  FolderIcon,
  GridIcon,
  LayoutIcon,
  PlayIcon,
  SaveIcon,
  UploadIcon,
} from "./icons";
import { CompositionInspector, NodeInspector } from "./Inspector";
import { capabilitiesFrom, idStem, newNode } from "./kinds";
import { Palette, TemplateGallery } from "./Library";
import { McpInspector } from "./McpInspector";
import { RunPanel } from "./RunPanel";
import { isFinished, waitingApprovals } from "./runState";
import { TEMPLATES, type Template } from "./templates";
import type {
  CompositionRecord,
  CompositionSpec,
  NodeKind,
  RunSummary,
  SpecNode,
  StudioRegistry,
  StudioSchema,
} from "./types";
import { useComposition, type Saved } from "./useComposition";
import { useStudioRun } from "./useStudioRun";
import { useNow } from "./useNow";
import { useTabs } from "./useTabs";
import { groupIssues, validate, type Issue } from "./validate";

type Tab = "inspect" | "run" | "a2a" | "mcp" | "history";

const TABS: { id: Tab; label: string }[] = [
  { id: "inspect", label: "Inspect" },
  { id: "run", label: "Run" },
  { id: "a2a", label: "A2A" },
  { id: "mcp", label: "MCP" },
  { id: "history", label: "History" },
];

const TAB_IDS = TABS.map((t) => t.id);

const EMPTY: CompositionSpec = {
  spec_version: 1,
  name: "untitled",
  pattern: "pipeline",
  nodes: [],
  edges: [],
};

/**
 * A server error, pinned to what it was about. It stays on show while that
 * thing (the node, or the rest of the spec) is unchanged, and goes once it is
 * edited: from then on the client's own checks speak until the next run.
 */
interface PinnedIssue {
  issue: Issue;
  nodeId: string | null;
  anchor: string;
}

function anchorOf(
  issue: Issue,
  spec: CompositionSpec,
  input: string,
): { nodeId: string | null; anchor: string } {
  if (issue.loc[0] === "input") return { nodeId: null, anchor: `input:${input}` };
  if (issue.loc[0] === "nodes" && typeof issue.loc[1] === "number") {
    const node = spec.nodes[issue.loc[1]];
    if (node) return { nodeId: node.id, anchor: JSON.stringify(node) };
  }
  return { nodeId: null, anchor: JSON.stringify({ ...spec, nodes: undefined }) };
}

function stillPinned(p: PinnedIssue, spec: CompositionSpec, input: string): Issue | null {
  if (p.issue.loc[0] === "input") return p.anchor === `input:${input}` ? p.issue : null;
  if (p.nodeId !== null) {
    const at = spec.nodes.findIndex((n) => n.id === p.nodeId);
    if (at === -1 || JSON.stringify(spec.nodes[at]) !== p.anchor) return null;
    return { ...p.issue, loc: ["nodes", at, ...p.issue.loc.slice(2)] };
  }
  return JSON.stringify({ ...spec, nodes: undefined }) === p.anchor ? p.issue : null;
}

interface Props {
  token: string;
  /**
   * False while the console shows another view: the Studio stays mounted (a
   * run keeps streaming) but stops its clock, so it does not re-render.
   */
  active?: boolean;
  /**
   * How many decisions the live run waits for (approvals, a remote's
   * question): the console shows it outside the Studio, so a run is not left
   * to expire while its person is in the chat or another tab.
   */
  onAttention?: (waiting: number) => void;
}

/**
 * Orrery Studio (AEP-031 P1): compose agents, remote A2A agents and MCP
 * tools on a canvas, run the composition apart from the live system, and
 * watch it run. Admins only; lazy-loaded, so none of it is in the chat
 * console's first load.
 */
export default function StudioView({ token, active = true, onAttention }: Props) {
  // One client for the view's life: a silent token renewal swaps its token
  // instead of making a new client, which would refetch the registry (losing
  // resolved cards) and strand a stream being followed on the old token.
  const client = useMemo(() => new StudioClient(token), []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    client.setToken(token);
  }, [client, token]);
  const [schema, setSchema] = useState<StudioSchema | null>(null);
  const [unavailable, setUnavailable] = useState<{ status: number; message: string } | null>(null);
  // Until the schema answers, it is not known whether the Studio is mounted here.
  const [schemaSettled, setSchemaSettled] = useState(false);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [registry, setRegistry] = useState<StudioRegistry | null>(null);
  const [registryError, setRegistryError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  const [graph, setGraph] = useState<StudioGraph>(() => specToGraph(EMPTY));
  const [undo, setUndo] = useState<{
    graph: StudioGraph;
    label: string;
    saved: Saved | null;
  } | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("inspect");
  const [input, setInput] = useState("");
  const [pinned, setPinned] = useState<PinnedIssue[]>([]);
  const [dialog, setDialog] = useState<
    null | "templates" | "export" | "import" | "registry" | "open"
  >(null);
  const [saveProblem, setSaveProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | undefined>(undefined);
  const panelRef = useRef<HTMLDivElement>(null);
  const directionRef = useRef<Direction>("LR");
  // Bumped when the graph is replaced or laid out again, so the canvas refits.
  const [fitSignal, setFitSignal] = useState(0);
  const refit = useCallback(() => setFitSignal((n) => n + 1), []);
  const caps = useMemo(() => capabilitiesFrom(schema?.capabilities), [schema]);
  const run = useStudioRun(client);
  const wide = useMediaQuery(BREAKPOINT_LG);
  const live = run.busy;
  const editable = !live;
  const now = useNow(live && active);
  // Bumped to move focus to the run input (once the Run tab has rendered).
  const [focusInput, setFocusInput] = useState(0);
  const tabs = useTabs(TAB_IDS, tab, setTab);

  // A live region only speaks when its text changes: clear it first, so the
  // same message twice ("Added writer_2." after an undo) is announced twice.
  const announce = useCallback((message: string) => {
    setNotice(null);
    window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(message), 50);
  }, []);
  useEffect(() => () => window.clearTimeout(noticeTimer.current), []);

  /** Where focus goes when the thing holding it is deleted: the panel beside the canvas. */
  const focusPanel = useCallback(() => {
    requestAnimationFrame(() => panelRef.current?.focus());
  }, []);

  // A new direction lays out only a graph nobody has arranged by hand (see
  // reorient); otherwise it waits for the next load or Tidy.
  const onDirection = useCallback(
    (direction: Direction) => {
      if (direction === directionRef.current) return;
      directionRef.current = direction;
      const next = reorient(graph, direction);
      if (next === graph) return;
      setGraph(next);
      if (next.nodes.length) refit();
    },
    [graph, refit],
  );

  useEffect(() => {
    const controller = new AbortController();
    const loadSchema = async () => {
      // A blip should not leave the console guessing what the server runs:
      // try again (after 1 s, then 3 s) before settling for the client's rules.
      for (const delay of [1000, 3000, null]) {
        try {
          setSchema(await client.schema(controller.signal));
          setSchemaError(null);
          return;
        } catch (err) {
          if (controller.signal.aborted) return;
          if (err instanceof ApiError && (err.status === 404 || err.status === 403)) {
            setUnavailable({ status: err.status, message: err.message });
            return;
          }
          // Open the canvas now; a later answer still brings the capabilities.
          setSchemaSettled(true);
          if (delay === null) {
            // The canvas still works: only the schema half of live validation is missing.
            setSchemaError(
              `${describeApiError(err, "The Studio's schema could not be read.")} Only this release's own rules are checked here.`,
            );
            return;
          }
          await new Promise((resolve) => window.setTimeout(resolve, delay));
          if (controller.signal.aborted) return;
        }
      }
    };
    void loadSchema().finally(() => {
      if (!controller.signal.aborted) setSchemaSettled(true);
    });
    client
      .registry(false, controller.signal)
      .then(setRegistry)
      .catch((err: unknown) => {
        if (!controller.signal.aborted)
          setRegistryError(describeApiError(err, "The registry could not be read."));
      });
    return () => controller.abort();
  }, [client]);

  const resolveCards = useCallback(async () => {
    setResolving(true);
    try {
      setRegistry(await client.registry(true));
      setRegistryError(null);
    } catch (err) {
      setRegistryError(describeApiError(err, "The registry could not be read."));
    } finally {
      setResolving(false);
    }
  }, [client]);

  const spec = useMemo(() => graphToSpec(graph), [graph]);
  const composition = useComposition(client, spec);
  const clientIssues = useMemo(
    () => (spec.nodes.length ? validate(spec, schema?.spec ?? null, { caps, registry }) : []),
    [spec, schema, caps, registry],
  );
  const serverIssues = useMemo(
    () => pinned.flatMap((p) => stillPinned(p, spec, input) ?? []),
    [pinned, spec, input],
  );
  const issues = useMemo(
    () => groupIssues([...clientIssues, ...serverIssues], spec),
    [clientIssues, serverIssues, spec],
  );

  const edit = useCallback((update: (g: StudioGraph) => StudioGraph) => setGraph(update), []);

  /**
   * Put *next* on the canvas, keeping the previous canvas (and the saved
   * composition it belonged to) for Undo. *saved* is what *next* belongs to:
   * nothing for a template or an import, so a Save never overwrites the
   * composition that was open with something else.
   */
  /** Bumped by anything that changes the canvas: a Replay still fetching then gives up. */
  const openRequest = useRef(0);
  const replaceGraph = useCallback(
    (next: StudioGraph, label: string, saved: Saved | null = null) => {
      openRequest.current += 1;
      setUndo(graph.nodes.length ? { graph, label, saved: composition.saved } : null);
      composition.restore(saved);
      setSaveProblem(null);
      setGraph(next);
      setSelectedId(null);
      setPinned([]);
      setDialog(null);
      refit();
    },
    [graph, refit, composition],
  );

  const loadTemplate = useCallback(
    (t: Template) => {
      replaceGraph(specToGraph(t.spec, directionRef.current), `Loaded ${t.title}`);
      announce(`Loaded the ${t.title} template.`);
    },
    [replaceGraph, announce],
  );

  const addKind = useCallback(
    (kind: NodeKind) => {
      const id = uniqueId(
        idStem(kind),
        graph.nodes.map((n) => n.id),
      );
      setGraph((g) => addNode(g, newNode(kind, id)));
      setSelectedId(id);
      setTab("inspect");
      announce(`Added ${id}.`);
    },
    [graph.nodes, announce],
  );

  const onRun = useCallback(async () => {
    if (!input.trim()) {
      setTab("run");
      setFocusInput((n) => n + 1);
      return;
    }
    setTab("run");
    setPinned([]);
    openRequest.current += 1;
    const refused = await run.start(spec, input, composition.runSource);
    if (refused?.length) {
      setPinned(refused.map((issue) => ({ issue, ...anchorOf(issue, spec, input) })));
      const first = refused.find((i) => i.loc[0] === "nodes" && typeof i.loc[1] === "number");
      const firstNode = first ? spec.nodes[first.loc[1] as number]?.id : undefined;
      if (refused.every((i) => i.loc[0] === "input")) setTab("run");
      else {
        setSelectedId(firstNode ?? null);
        setTab("inspect");
      }
      announce(
        `The server refused the composition: ${refused.length} problem${refused.length === 1 ? "" : "s"}, marked on the canvas.`,
      );
    }
  }, [input, run, spec, announce, composition.runSource]);

  const showRun = useCallback(
    (runId: string) => {
      const record = run.history.find((r) => r.runId === runId);
      if (record?.spec && JSON.stringify(record.spec) !== JSON.stringify(spec)) {
        const next = specToGraph(record.spec, directionRef.current);
        // A run of a saved version stays linked to it; a save is then checked
        // against that version, so a newer one is a conflict, not overwritten.
        const source = record.source;
        replaceGraph(
          next,
          "Opened a past run",
          source
            ? {
                id: source.compositionId,
                version: source.version,
                latest: source.version,
                versions: [source.version],
                baseline: JSON.stringify(graphToSpec(next)),
              }
            : null,
        );
      }
      run.show(runId);
      setTab("run");
    },
    [run, spec, replaceGraph],
  );

  const liveView = run.history.find((r) => r.runId === run.liveRunId)?.view ?? null;
  const waiting = liveView ? waitingApprovals(liveView).length : 0;
  useEffect(() => {
    onAttention?.(waiting);
  }, [onAttention, waiting]);
  useEffect(() => () => onAttention?.(0), [onAttention]);

  /** Open a saved composition (one version of it) on the canvas. */
  const openComposition = useCallback(
    (record: CompositionRecord, label = `Opened ${record.name} v${record.version}`) => {
      const next = specToGraph(record.spec, directionRef.current);
      replaceGraph(next, label);
      composition.adopt(record, graphToSpec(next));
      announce(`${label}.`);
    },
    [replaceGraph, composition, announce],
  );

  /** A run from the server's list: its composition version on the canvas when known, then replayed. */
  const openSavedRun = useCallback(
    async (summary: RunSummary) => {
      // Two quick Replays: only the last one asked for reaches the canvas.
      const request = ++openRequest.current;
      if (run.history.some((r) => r.runId === summary.run_id)) {
        showRun(summary.run_id);
        return;
      }
      let spec: CompositionSpec | null = null;
      if (summary.composition_id && summary.composition_version) {
        try {
          const record = await client.composition(
            summary.composition_id,
            summary.composition_version,
          );
          if (request !== openRequest.current) return;
          spec = record.spec;
          openComposition(
            record,
            `Opened the run's composition, ${record.name} v${record.version}`,
          );
        } catch {
          // Deleted since: the run still replays, its nodes marked where ids match.
          if (request !== openRequest.current) return;
        }
      }
      run.open(summary, spec);
      setTab("run");
      if (!spec) {
        announce(
          "This run's composition is not saved, so the canvas is unchanged: nodes with the same ids show its progress.",
        );
      }
    },
    [client, run, showRun, openComposition, announce],
  );

  const onSave = useCallback(
    async (asNew = false) => {
      setSaveProblem(null);
      const result = await composition.save(spec, asNew);
      if (result.ok) {
        announce(`Saved ${spec.name}.`);
        return;
      }
      if ("skipped" in result) return;
      if ("issues" in result) {
        setPinned(result.issues.map((issue) => ({ issue, ...anchorOf(issue, spec, input) })));
        setTab("inspect");
        setSaveProblem(
          `Not saved: the server found ${result.issues.length} problem${result.issues.length === 1 ? "" : "s"}, marked on the canvas.`,
        );
      } else if ("conflict" in result) {
        setSaveProblem(
          `Not saved: ${spec.name} has a newer version than the one on the canvas${result.conflict ? ` (v${result.conflict})` : ""}. Save a copy, or open the newest version and redo your change.`,
        );
      } else {
        setSaveProblem(result.error);
      }
    },
    [composition, spec, input, announce],
  );

  // Ctrl/Cmd+S saves, rather than the browser saving the page.
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (!dialog && !e.repeat) void onSave(e.shiftKey);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, dialog, onSave]);

  const selected = graph.nodes.find((n) => n.id === selectedId)?.data.spec ?? null;
  const problemCount = issues.count;
  const view = run.view;

  if (!schemaSettled) {
    return <p className="p-6 text-sm text-slate-500 dark:text-slate-400">Loading the Studio…</p>;
  }

  if (unavailable) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="max-w-md text-center">
          <span className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-brand-50 text-brand-700 dark:bg-brand-950/70 dark:text-brand-200">
            <StudioIcon size={24} />
          </span>
          <h2 className="mt-3 font-semibold text-slate-900 dark:text-slate-100">
            {unavailable.status === 404
              ? "The Studio is not enabled here"
              : "The Studio is for people"}
          </h2>
          <p className="mt-1.5 text-sm text-slate-600 dark:text-slate-400">
            {unavailable.status === 404 ? (
              <>
                This deployment does not mount <code className="font-mono">/studio</code>. An
                operator turns it on with{" "}
                <code className="font-mono">ORRERY_STUDIO_ENABLED=true</code> (it needs auth and{" "}
                <code className="font-mono">ORRERY_HUMAN_CLIENTS</code>).
              </>
            ) : (
              <>
                The server refused this session: {unavailable.message}. The Studio needs the admin
                role and a token whose client is listed in{" "}
                <code className="font-mono">ORRERY_HUMAN_CLIENTS</code>.
              </>
            )}
          </p>
        </div>
      </div>
    );
  }

  const emptyState = (
    <div className="w-[min(36rem,calc(100vw-2rem))] rounded-2xl border border-slate-200 bg-white/90 p-4 shadow-xl backdrop-blur dark:border-slate-800 dark:bg-slate-900/90">
      <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">Compose a run</p>
      <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
        Start from a pattern, or {wide ? "drag a node from the palette" : "add a node"}. Each node
        is an agent with an instruction and the registry tools it may call.
      </p>
      <div className="mt-3">
        <TemplateGallery
          templates={TEMPLATES.slice(0, 4)}
          caps={caps}
          onPick={loadTemplate}
          compact
        />
      </div>
      <button
        type="button"
        className={`${toolbarButton} mt-2 -ml-2 text-xs`}
        onClick={() => setDialog("templates")}
      >
        <GridIcon size={14} /> All patterns
      </button>
    </div>
  );

  return (
    <ReactFlowProvider>
      <div className="flex min-h-0 flex-1 flex-col">
        <p role="status" className="sr-only">
          {notice}
          {view ? ` Run ${runLabel[view.status]}.` : ""}
        </p>
        <div className="flex flex-wrap items-center gap-1.5 border-b border-slate-200/80 bg-white/70 px-3 py-2 backdrop-blur sm:px-4 dark:border-slate-800/80 dark:bg-slate-900/70">
          <span className="flex min-w-0 items-center gap-2">
            <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-linear-to-br from-brand-500 to-brand-700 text-white shadow-sm">
              <StudioIcon size={16} />
            </span>
            <span className="min-w-0">
              <span className="block truncate font-mono text-sm font-semibold text-slate-900 dark:text-slate-100">
                {graph.meta.name}
              </span>
              <span className="block text-[10px] font-medium tracking-wide text-slate-500 uppercase dark:text-slate-400">
                {(graph.meta.pattern ?? "pipeline").replace(/_/g, " ")} · {graph.nodes.length} node
                {graph.nodes.length === 1 ? "" : "s"}
                {composition.saved ? ` · v${composition.saved.version}` : ""}
                {composition.saved && composition.dirty ? (
                  <span className="ml-1 text-warn-fg normal-case">· unsaved changes</span>
                ) : null}
              </span>
            </span>
          </span>
          {graph.nodes.length ? (
            <button
              type="button"
              onClick={() => {
                setSelectedId(null);
                setTab("inspect");
              }}
              className={`ml-1 inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none ${
                problemCount ? toneChip.crit : toneChip.ok
              }`}
            >
              {problemCount ? <AlertIcon size={12} /> : <CheckIcon size={12} />}
              {problemCount ? `${problemCount} problem${problemCount === 1 ? "" : "s"}` : "Valid"}
            </button>
          ) : null}
          {live ? (
            <span
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${tonePill.running}`}
            >
              <LockIcon size={11} /> locked while running
            </span>
          ) : null}
          <span className="ml-auto flex flex-wrap items-center gap-0.5">
            <button
              type="button"
              className={toolbarButton}
              disabled={live || composition.saving}
              onClick={() => setDialog("open")}
              title="Open a saved composition"
            >
              <FolderIcon size={15} />
              <span className="sr-only sm:not-sr-only">Open</span>
            </button>
            <button
              type="button"
              className={toolbarButton}
              disabled={!composition.canSave}
              onClick={() => void onSave()}
              title={
                composition.saved
                  ? `Save a new version of ${spec.name} (Ctrl+S; Ctrl+Shift+S saves a copy)`
                  : "Save on the server, for every Studio admin (Ctrl+S)"
              }
            >
              <SaveIcon size={15} />
              <span className="sr-only sm:not-sr-only">
                {composition.saving
                  ? "Saving…"
                  : composition.saved && composition.saved.version < composition.saved.latest
                    ? "Restore"
                    : "Save"}
              </span>
            </button>
            {composition.saved ? (
              <button
                type="button"
                className={toolbarButton}
                disabled={!graph.nodes.length || composition.saving}
                onClick={() => void onSave(true)}
                title="Save the canvas as a new composition"
              >
                <span className="sr-only sm:not-sr-only">Save copy</span>
                <span className="sm:hidden" aria-hidden="true">
                  +
                </span>
              </button>
            ) : null}
            <button
              type="button"
              className={toolbarButton}
              disabled={live || composition.saving}
              onClick={() => setDialog("templates")}
            >
              <GridIcon size={15} />
              <span className="sr-only sm:not-sr-only">Patterns</span>
            </button>
            <button
              type="button"
              className={toolbarButton}
              disabled={live || !graph.nodes.length}
              onClick={() => {
                setGraph((g) => relayout(g, directionRef.current));
                refit();
              }}
              title="Lay the graph out again"
            >
              <LayoutIcon size={15} />
              <span className="sr-only sm:not-sr-only">Tidy</span>
            </button>
            <button
              type="button"
              className={toolbarButton}
              onClick={() => setDialog("registry")}
              title="The MCP servers and remote agents compositions may reach"
            >
              <GridIcon size={15} />
              <span className="sr-only sm:not-sr-only">Registry</span>
            </button>
            <button
              type="button"
              className={toolbarButton}
              disabled={live || composition.saving}
              onClick={() => setDialog("import")}
            >
              <UploadIcon size={15} />
              <span className="sr-only sm:not-sr-only">Import</span>
            </button>
            <button
              type="button"
              className={toolbarButton}
              disabled={!graph.nodes.length}
              onClick={() => setDialog("export")}
            >
              <DownloadIcon size={15} />
              <span className="sr-only sm:not-sr-only">Export</span>
            </button>
            <button
              type="button"
              className={`${primaryButton} ml-1`}
              disabled={live || !graph.nodes.length}
              onClick={() => void onRun()}
            >
              <PlayIcon size={14} /> Run
            </button>
          </span>
        </div>

        {undo ? (
          <div
            role="status"
            className="flex items-center gap-2 border-b border-slate-200 bg-brand-50 px-4 py-1.5 text-xs text-brand-900 dark:border-slate-800 dark:bg-brand-950/50 dark:text-brand-100"
          >
            <span>{undo.label}: the previous canvas is kept.</span>
            <button
              type="button"
              className="font-semibold underline underline-offset-2 disabled:opacity-50"
              disabled={composition.saving || live}
              onClick={() => {
                openRequest.current += 1;
                setGraph(reorient(undo.graph, directionRef.current));
                composition.restore(undo.saved);
                setUndo(null);
                setSelectedId(null);
                refit();
              }}
            >
              Undo
            </button>
            <button
              type="button"
              aria-label="Dismiss"
              className="ml-auto rounded p-0.5 opacity-70 hover:opacity-100"
              onClick={() => setUndo(null)}
            >
              <CloseIcon size={12} />
            </button>
          </div>
        ) : null}
        {saveProblem ? (
          <div
            role="alert"
            className="flex items-start gap-2 border-b border-crit-border bg-crit-bg px-4 py-1.5 text-xs text-crit-fg"
          >
            <span className="flex-1">{saveProblem}</span>
            <button
              type="button"
              aria-label="Dismiss"
              className="rounded p-0.5 opacity-70 hover:opacity-100"
              onClick={() => setSaveProblem(null)}
            >
              <CloseIcon size={12} />
            </button>
          </div>
        ) : null}
        {registryError || schemaError ? (
          <p
            role="alert"
            className="border-b border-warn-border bg-warn-bg px-4 py-1.5 text-xs text-warn-fg"
          >
            {registryError
              ? `${registryError} Tool references can't be checked until it loads. `
              : ""}
            {schemaError}
          </p>
        ) : null}

        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden">
          {wide ? (
            <aside
              aria-label="Palette"
              className="orrery-scroll w-14 shrink-0 overflow-y-auto border-r border-slate-200 bg-white/60 p-2 xl:w-60 xl:p-3 dark:border-slate-800 dark:bg-slate-900/40"
            >
              <Palette caps={caps} editable={editable} onAdd={addKind} />
              <p
                className={`${sectionTitle} mt-5 hidden items-center justify-between px-1 xl:flex`}
              >
                Registry
                <button
                  type="button"
                  className="text-[11px] font-medium tracking-normal text-brand-600 normal-case underline dark:text-brand-400"
                  onClick={() => setDialog("registry")}
                >
                  {registry?.editable ? "Edit" : "View"}
                </button>
              </p>
              <ul className="mt-1.5 hidden flex-col gap-1 px-1 text-xs text-slate-600 xl:flex dark:text-slate-300">
                <li className="flex justify-between">
                  <span>MCP servers</span>
                  <span className="font-mono tabular-nums">{registry?.mcp.length ?? "…"}</span>
                </li>
                <li className="flex justify-between">
                  <span>Remote agents</span>
                  <span className="font-mono tabular-nums">{registry?.a2a.length ?? "…"}</span>
                </li>
                <li className="flex justify-between">
                  <span>Catalogue tools</span>
                  <span className="font-mono tabular-nums">
                    {registry?.catalog.filter((t) => t.read_only).length ?? "…"}
                  </span>
                </li>
              </ul>
              <p className="mt-4 hidden px-1 text-[11px] leading-relaxed text-slate-500 xl:block dark:text-slate-400">
                Runs are read-only (L2) and never touch the live agents, catalogue, chats or memory.
              </p>
            </aside>
          ) : null}

          <section
            aria-label="Canvas"
            className="relative h-[22rem] shrink-0 border-b border-slate-200 bg-slate-50 lg:h-auto lg:min-w-0 lg:flex-1 lg:border-b-0 dark:border-slate-800 dark:bg-slate-950"
          >
            <Canvas
              graph={graph}
              onGraph={edit}
              issues={issues}
              run={view}
              caps={caps}
              editable={editable && wide}
              selectedId={selectedId}
              onSelect={(id) => {
                setSelectedId(id);
                if (id) setTab("inspect");
              }}
              now={now}
              empty={emptyState}
              fitSignal={fitSignal}
              onDirection={onDirection}
              onRemoved={() => {
                setTab("inspect");
                announce("Removed from the composition.");
                focusPanel();
              }}
            />
            {!wide && graph.nodes.length ? (
              <p className="pointer-events-none absolute top-2 left-1/2 -translate-x-1/2 rounded-full bg-white/90 px-2.5 py-1 text-[11px] text-slate-600 shadow-sm dark:bg-slate-900/90 dark:text-slate-300">
                Tap a node to inspect it · drag-to-edit needs a wider screen
              </p>
            ) : null}
          </section>

          <aside
            aria-label="Studio panels"
            className="flex shrink-0 flex-col bg-white lg:w-[22rem] lg:border-l 2xl:w-[25rem] lg:border-slate-200 dark:bg-slate-900 lg:dark:border-slate-800"
          >
            <div
              role="tablist"
              aria-label="Studio panels"
              onKeyDown={tabs.onKeyDown}
              className="flex gap-0.5 overflow-x-auto border-b border-slate-200 px-2 pt-1.5 dark:border-slate-800"
            >
              {TABS.map((t) => {
                const badge =
                  t.id === "run" && waiting
                    ? `${waiting} waiting`
                    : t.id === "run" && view
                      ? isFinished(view)
                        ? null
                        : "●"
                      : t.id === "a2a" && view?.a2a.length
                        ? String(view.a2a.length)
                        : t.id === "mcp" && view?.mcp.length
                          ? String(view.mcp.length)
                          : t.id === "history" && run.history.length
                            ? String(run.history.length)
                            : null;
                return (
                  <button
                    key={t.id}
                    type="button"
                    {...tabs.tabProps(t.id)}
                    id={`studio-tab-${t.id}`}
                    aria-controls="studio-tabpanel"
                    className={`relative -mb-px inline-flex items-center gap-1 border-b-2 px-2 py-2 text-[13px] font-medium whitespace-nowrap 2xl:px-2.5 2xl:text-sm focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none ${
                      tab === t.id
                        ? "border-brand-600 text-slate-900 dark:border-brand-400 dark:text-white"
                        : "border-transparent text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-100"
                    }`}
                  >
                    {t.label}
                    {badge ? <span className="sr-only">, </span> : null}
                    {badge ? (
                      <span
                        className={`rounded-full px-1.5 text-[10px] tabular-nums ${badge === "●" ? "text-running-fg motion-safe:animate-pulse" : t.id === "run" && waiting ? `font-semibold ${tonePill.warn}` : "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300"}`}
                      >
                        {badge}
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>
            <div
              ref={panelRef}
              id="studio-tabpanel"
              role="tabpanel"
              tabIndex={0}
              aria-labelledby={`studio-tab-${tab}`}
              className="orrery-scroll min-h-0 flex-1 p-4 lg:overflow-y-auto"
            >
              {waiting && view?.runId !== run.liveRunId && run.liveRunId ? (
                // The badge counts the live run's decisions; its cards are
                // in the Run tab only while it is the run on show.
                <div
                  role="alert"
                  className={`${panelCard} mb-3 flex items-center gap-2 border-warn-border bg-warn-bg p-2.5 text-xs text-warn-fg`}
                >
                  <span className="flex-1">
                    The live run is waiting for you ({waiting}). Decide before it expires.
                  </span>
                  <button
                    type="button"
                    className="font-semibold underline underline-offset-2"
                    onClick={() => showRun(run.liveRunId as string)}
                  >
                    Go to the live run
                  </button>
                </div>
              ) : null}
              {tab === "inspect" ? (
                selected ? (
                  <NodeInspector
                    key={selected.id}
                    graph={graph}
                    node={selected}
                    issues={issues.byNode.get(selected.id) ?? []}
                    registry={registry}
                    caps={caps}
                    editable={editable}
                    onChange={(id: string, next: SpecNode) => {
                      setGraph((g) => updateNode(g, id, next));
                      if (next.id !== id) setSelectedId(next.id);
                    }}
                    onRemove={(id) => {
                      setGraph((g) => removeNodes(g, [id]));
                      setSelectedId(null);
                      announce(`Removed ${id}.`);
                      focusPanel();
                    }}
                    onConnect={(from, to) => setGraph((g) => connect(g, from, to))}
                    onDisconnect={(edgeId) => setGraph((g) => removeEdges(g, [edgeId]))}
                    onRelabel={(edgeId, when) => setGraph((g) => relabel(g, edgeId, when))}
                  />
                ) : (
                  <CompositionInspector
                    graph={graph}
                    issues={issues}
                    caps={caps}
                    editable={editable}
                    onMeta={(meta) => setGraph((g) => ({ ...g, meta }))}
                    onSelectNode={setSelectedId}
                  />
                )
              ) : tab === "run" ? (
                <RunPanel
                  client={client}
                  view={view}
                  input={input}
                  onInput={setInput}
                  focusSignal={focusInput}
                  inputIssues={issues.input}
                  problems={clientIssues.length}
                  busy={run.busy}
                  cancelling={run.cancelling}
                  connection={view && view.runId === run.liveRunId ? run.connection : "idle"}
                  error={run.error}
                  onClearError={run.clearError}
                  onRun={() => void onRun()}
                  onCancel={() => void run.cancel()}
                  onReconnect={run.reconnect}
                  now={now}
                />
              ) : tab === "a2a" ? (
                <A2aInspector
                  registry={registry}
                  view={view}
                  resolving={resolving}
                  onResolve={() => void resolveCards()}
                />
              ) : tab === "mcp" ? (
                <McpInspector registry={registry} view={view} />
              ) : (
                <HistoryPanel
                  client={client}
                  records={run.history}
                  shownId={view?.runId ?? null}
                  compositionId={composition.saved?.id ?? null}
                  locked={live}
                  // While a start is out, the last live run is over: nothing is exempt.
                  liveRunId={
                    run.history.some((r) => r.runId === run.liveRunId && !isFinished(r.view))
                      ? run.liveRunId
                      : null
                  }
                  onShow={showRun}
                  onOpenSaved={(summary) => void openSavedRun(summary)}
                />
              )}
            </div>
          </aside>
        </div>
      </div>

      {dialog === "open" ? (
        <CompositionsDialog
          client={client}
          currentId={composition.saved?.id ?? null}
          onOpen={(record) => openComposition(record)}
          onDeleted={(id) => {
            // The canvas stays, unlinked: Save makes it a new composition.
            if (composition.saved?.id === id) composition.restore(null);
            setUndo((u) => (u?.saved?.id === id ? { ...u, saved: null } : u));
          }}
          onClose={() => setDialog(null)}
        />
      ) : null}
      {dialog === "templates" ? (
        <TemplatesDialog
          templates={TEMPLATES}
          caps={caps}
          onPick={loadTemplate}
          onClose={() => setDialog(null)}
        />
      ) : null}
      {dialog === "registry" ? (
        <RegistryDialog
          client={client}
          onSaved={() => {
            client
              .registry()
              .then(setRegistry)
              .catch(() => {});
          }}
          onClose={() => setDialog(null)}
        />
      ) : null}
      {dialog === "export" || dialog === "import" ? (
        <SpecDialog
          spec={spec}
          initialTab={dialog}
          onClose={() => setDialog(null)}
          onImport={(imported, notes) => {
            replaceGraph(specToGraph(imported, directionRef.current), `Imported ${imported.name}`);
            announce([`Imported ${imported.name}.`, ...notes].join(" "));
          }}
        />
      ) : null}
    </ReactFlowProvider>
  );
}
