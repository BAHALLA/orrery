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
import { SpecDialog, TemplatesDialog } from "./Dialogs";
import { primaryButton, runLabel, sectionTitle, toolbarButton } from "./format";
import {
  addNode,
  connect,
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
import { DownloadIcon, GridIcon, LayoutIcon, PlayIcon, UploadIcon } from "./icons";
import { CompositionInspector, NodeInspector } from "./Inspector";
import { idStem, newNode, P0A } from "./kinds";
import { Palette, TemplateGallery } from "./Library";
import { McpInspector } from "./McpInspector";
import { RunPanel } from "./RunPanel";
import { isFinished } from "./runState";
import { TEMPLATES, type Template } from "./templates";
import type { CompositionSpec, NodeKind, SpecNode, StudioRegistry, StudioSchema } from "./types";
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
}

/**
 * Orrery Studio (AEP-031 P1): compose agents, remote A2A agents and MCP
 * tools on a canvas, run the composition apart from the live system, and
 * watch it run. Admins only; lazy-loaded, so none of it is in the chat
 * console's first load.
 */
export default function StudioView({ token, active = true }: Props) {
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
  const [undo, setUndo] = useState<{ graph: StudioGraph; label: string } | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("inspect");
  const [input, setInput] = useState("");
  const [pinned, setPinned] = useState<PinnedIssue[]>([]);
  const [dialog, setDialog] = useState<null | "templates" | "export" | "import">(null);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | undefined>(undefined);
  const panelRef = useRef<HTMLDivElement>(null);
  const directionRef = useRef<Direction>("LR");
  // Bumped when the graph is replaced or laid out again, so the canvas refits.
  const [fitSignal, setFitSignal] = useState(0);
  const refit = useCallback(() => setFitSignal((n) => n + 1), []);
  const caps = P0A;
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
    client
      .schema(controller.signal)
      .then(setSchema)
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 403)) {
          setUnavailable({ status: err.status, message: err.message });
        } else {
          // The canvas still works: only the schema half of live validation is missing.
          setSchemaError(
            `${describeApiError(err, "The Studio's schema could not be read.")} Only this release's own rules are checked here.`,
          );
        }
      })
      .finally(() => {
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

  const replaceGraph = useCallback(
    (next: StudioGraph, label: string) => {
      setUndo(graph.nodes.length ? { graph, label } : null);
      setGraph(next);
      setSelectedId(null);
      setPinned([]);
      setDialog(null);
      refit();
    },
    [graph, refit],
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
    const refused = await run.start(spec, input);
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
  }, [input, run, spec, announce]);

  const showRun = useCallback(
    (runId: string) => {
      const record = run.history.find((r) => r.runId === runId);
      if (record && JSON.stringify(record.spec) !== JSON.stringify(spec)) {
        replaceGraph(specToGraph(record.spec, directionRef.current), "Opened a past run");
      }
      run.show(runId);
      setTab("run");
    },
    [run, spec, replaceGraph],
  );

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
              disabled={live}
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
              disabled={live}
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
              className="font-semibold underline underline-offset-2"
              onClick={() => {
                setGraph(reorient(undo.graph, directionRef.current));
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
              <p className={`${sectionTitle} mt-5 hidden px-1 xl:block`}>Registry</p>
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
                  t.id === "run" && view
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
                    {badge ? (
                      <span
                        className={`rounded-full px-1.5 text-[10px] tabular-nums ${badge === "●" ? "text-running-fg motion-safe:animate-pulse" : "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300"}`}
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
                  records={run.history}
                  shownId={view?.runId ?? null}
                  onShow={showRun}
                />
              )}
            </div>
          </aside>
        </div>
      </div>

      {dialog === "templates" ? (
        <TemplatesDialog
          templates={TEMPLATES}
          caps={caps}
          onPick={loadTemplate}
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
