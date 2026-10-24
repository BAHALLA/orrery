import { useCallback, useEffect, useMemo, useRef, type DragEvent, type ReactNode } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
  useStore,
  type Connection,
  type EdgeChange,
  type NodeChange,
  type ReactFlowState,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./studio.css";
import {
  addNode,
  connect,
  fitTo,
  moveNodes,
  removeEdges,
  removeNodes,
  uniqueId,
  type Direction,
  type StudioEdge,
  type StudioGraph,
  type StudioNode,
} from "./graph";
import { comingIn, idStem, KINDS, newNode, type Capabilities } from "./kinds";
import { NodeCard } from "./NodeCard";
import type { RunView } from "./runState";
import type { NodeKind } from "./types";
import type { IssueMap } from "./validate";

/** The drag payload a palette item carries. */
export const KIND_MIME = "application/x-orrery-studio-kind";

const nodeTypes = { studio: NodeCard };

interface Props {
  graph: StudioGraph;
  onGraph: (update: (graph: StudioGraph) => StudioGraph) => void;
  issues: IssueMap;
  run: RunView | null;
  caps: Capabilities;
  /** False on narrow screens: the graph is shown, not edited (the inspector still edits). */
  editable: boolean;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  /** Unix seconds, ticking while a run is live. */
  now: number;
  /** Shown over an empty canvas. */
  empty: ReactNode;
  /** Bumped whenever the graph is replaced or laid out again: the view refits. */
  fitSignal: number;
  /** The direction this canvas suits: top to bottom when it is taller than wide. */
  onDirection: (direction: Direction) => void;
  /** Nodes were deleted from the canvas (their focus is gone with them). */
  onRemoved: () => void;
}

/**
 * Fit a graph so its text stays readable: never below 0.75 (pan for the
 * rest), never above 1. FIT is React Flow's own fit (the controls' button);
 * FIT_TO is the Studio's, which also keeps the first rank in view.
 */
const FIT = { padding: 0.15, minZoom: 0.75, maxZoom: 1 };
const FIT_TO = { pad: 32, minZoom: 0.75, maxZoom: 1 };

const ariaLabels = {
  "node.a11yDescription.default":
    "Press enter or space to select a node; its inspector opens beside the canvas. Press delete to remove it.",
  "node.a11yDescription.keyboardDisabled":
    "Press enter or space to select a node. Use the arrow keys to move it. Press delete to remove it, escape to cancel.",
  "edge.a11yDescription.default":
    "Press enter or space to select an edge, then delete to remove it.",
  "controls.ariaLabel": "Canvas controls",
  "controls.zoomIn.ariaLabel": "Zoom in",
  "controls.zoomOut.ariaLabel": "Zoom out",
  "controls.fitView.ariaLabel": "Fit the composition in view",
  "controls.interactive.ariaLabel": "Lock or unlock editing",
  "minimap.ariaLabel": "Overview of the composition",
  "handle.ariaLabel": "Connection point",
};

/**
 * The composition canvas (React Flow).
 *
 * Holds no state: the graph lives in the Studio view, and every change comes
 * back through `onGraph` as a pure update from ./graph. Run state, problems
 * and "coming in P0b" are drawn onto copies of the nodes, never stored in
 * them, so they cannot leak into an exported spec.
 */
export function Canvas({
  graph,
  onGraph,
  issues,
  run,
  caps,
  editable,
  selectedId,
  onSelect,
  now,
  empty,
  fitSignal,
  onDirection,
  onRemoved,
}: Props) {
  const flow = useReactFlow();
  const vertical = graph.direction === "TB";

  // React Flow measures its pane; a canvas taller than wide lays out top to bottom.
  const tall = useStore((s) => (s.width && s.height ? s.height > s.width : null));
  useEffect(() => {
    if (tall !== null) onDirection(tall ? "TB" : "LR");
  }, [tall, onDirection]);

  // A replaced graph (a template, an import, a past run, a tidy, a new
  // direction) is fitted once its nodes are measured. React Flow's fitView
  // centres even when the graph overflows at its minimum zoom, cutting off
  // the first node; fitTo starts it at the margin instead.
  //
  // In the commit that swaps graphs, a value selected during render still
  // describes the old nodes, while the store already holds the new, unmeasured
  // ones. So the effect reads the store itself, skips the fit until every
  // current node has a size, and `measured` (which flips false, then true
  // once React Flow has measured them) runs it again.
  //
  // The view then stays fitted while the pane changes size (the undo banner
  // a load brings, a window resize) until the person pans or zooms.
  const measured = useStore(allMeasured);
  const pane = useStore((s) => ({ width: s.width, height: s.height }), shallowSize);
  const fitted = useRef({ signal: 0, pane: "" });
  const userMoved = useRef(false);
  useEffect(() => {
    if (!fitSignal || !measured || !pane.width || !pane.height) return;
    const paneKey = `${pane.width}x${pane.height}`;
    const done = fitted.current.signal === fitSignal;
    if (done && (fitted.current.pane === paneKey || userMoved.current)) return;
    const current = flow.getNodes();
    const ids = new Set(graph.nodes.map((n) => n.id));
    if (
      !current.length ||
      current.length !== ids.size ||
      current.some((n) => !ids.has(n.id) || !n.measured?.width || !n.measured.height)
    ) {
      return;
    }
    fitted.current = { signal: fitSignal, pane: paneKey };
    userMoved.current = false;
    void flow.setViewport(fitTo(flow.getNodesBounds(current), pane, FIT_TO), { duration: 200 });
  }, [fitSignal, measured, pane, flow, graph.nodes]);

  const nodes = useMemo<StudioNode[]>(
    () =>
      graph.nodes.map((n) => ({
        ...n,
        selected: n.id === selectedId,
        ariaLabel: nodeAria(n, issues, run),
        data: {
          ...n.data,
          issues: issues.byNode.get(n.id) ?? [],
          run: run?.nodes[n.id] ?? null,
          needs: comingIn(n.data.spec.kind, caps),
          now,
          vertical,
        },
      })),
    [graph.nodes, selectedId, issues, run, caps, now, vertical],
  );

  const edges = useMemo<StudioEdge[]>(() => {
    // Spec edges are numbered as the spec lists them; child links are not edges.
    const specEdges = graph.edges.flatMap((e) => (e.data?.child ? [] : [e.id]));
    return graph.edges.map((e) => {
      const problems = e.data?.child ? [] : (issues.byEdge.get(specEdges.indexOf(e.id)) ?? []);
      const source = run?.nodes[e.source]?.status;
      const target = run?.nodes[e.target]?.status;
      const live = run?.active === e.target && source === "ok";
      const done = source === "ok" && (target === "ok" || target === "active");
      const spec = e.data?.spec;
      const label = problems.length
        ? problems[0]?.msg
        : spec?.when
          ? `when ${spec.when}${spec.max_iterations ? ` · ≤${spec.max_iterations}×` : ""}`
          : undefined;
      return {
        ...e,
        animated: live,
        ...(label ? { label } : {}),
        className: [
          e.data?.child ? "studio-edge-child" : "",
          problems.length
            ? "studio-edge-issue"
            : live
              ? "studio-edge-live"
              : done
                ? "studio-edge-done"
                : "",
        ]
          .filter(Boolean)
          .join(" "),
        ariaLabel: `Edge from ${e.source} to ${e.target}${problems.length ? `: ${problems[0]?.msg}` : ""}`,
      };
    });
  }, [graph.edges, issues, run]);

  const onNodesChange = useCallback(
    (changes: NodeChange<StudioNode>[]) => {
      const removed = changes.flatMap((c) => (c.type === "remove" ? [c.id] : []));
      for (const c of changes) {
        if (c.type === "select" && c.selected) onSelect(c.id);
        if (c.type === "select" && !c.selected && c.id === selectedId) onSelect(null);
      }
      const rest = changes.filter((c) => c.type === "position" || c.type === "dimensions");
      if (!removed.length && !rest.length) return;
      // A position change is a drag or an arrow key: the graph is placed by
      // hand from then on. A dimensions change is only React Flow measuring.
      const byHand = rest.some((c) => c.type === "position");
      onGraph((g) => {
        const nodes = rest.length ? applyNodeChanges(rest, g.nodes) : g.nodes;
        const moved = byHand ? moveNodes(g, nodes) : rest.length ? { ...g, nodes } : g;
        return removed.length ? removeNodes(moved, removed) : moved;
      });
      if (selectedId && removed.includes(selectedId)) onSelect(null);
      if (removed.length) onRemoved();
    },
    [onGraph, onSelect, selectedId, onRemoved],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange<StudioEdge>[]) => {
      const removed = changes.flatMap((c) => (c.type === "remove" ? [c.id] : []));
      const select = changes.filter((c) => c.type === "select");
      onGraph((g) => {
        const next = select.length ? { ...g, edges: applyEdgeChanges(select, g.edges) } : g;
        return removed.length ? removeEdges(next, removed) : next;
      });
    },
    [onGraph],
  );

  const onConnect = useCallback(
    (c: Connection) => onGraph((g) => connect(g, c.source, c.target)),
    [onGraph],
  );

  const onDragOver = useCallback((e: DragEvent) => {
    if (!e.dataTransfer.types.includes(KIND_MIME)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }, []);

  const onDrop = useCallback(
    (e: DragEvent) => {
      const kind = e.dataTransfer.getData(KIND_MIME) as NodeKind;
      // Own keys only: `in` would take "constructor" or "__proto__" for a kind.
      if (!kind || !Object.hasOwn(KINDS, kind)) return;
      e.preventDefault();
      const position = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
      const id = uniqueId(
        idStem(kind),
        graph.nodes.map((n) => n.id),
      );
      onGraph((g) => addNode(g, newNode(kind, id), { x: position.x - 120, y: position.y - 40 }));
      onSelect(id);
    },
    [flow, graph.nodes, onGraph, onSelect],
  );

  return (
    <div
      className="relative h-full w-full"
      onDragOver={onDragOver}
      onDrop={editable ? onDrop : undefined}
    >
      <ReactFlow<StudioNode, StudioEdge>
        className="studio-flow"
        aria-label="Composition canvas"
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onPaneClick={() => onSelect(null)}
        nodesDraggable={editable}
        nodesConnectable={editable}
        deleteKeyCode={editable ? ["Backspace", "Delete"] : null}
        ariaLabelConfig={ariaLabels}
        fitViewOptions={FIT}
        // A pan or zoom by the person (an event; a fit has none) ends auto-fitting.
        onMoveStart={(event) => {
          if (event) userMoved.current = true;
        }}
        minZoom={0.3}
        maxZoom={1.75}
        defaultEdgeOptions={{ type: "smoothstep" }}
        proOptions={{ hideAttribution: false }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1.2} />
        <Controls showInteractive={false} position="bottom-left" />
        {/* A tall, narrow canvas has no room for it: it would sit on the graph. */}
        {vertical ? null : (
          <MiniMap
            pannable
            zoomable
            position="bottom-right"
            className="hidden! sm:block!"
            nodeBorderRadius={8}
          />
        )}
      </ReactFlow>
      {graph.nodes.length === 0 ? (
        <div className="pointer-events-none absolute inset-0 grid place-items-center p-4">
          <div className="pointer-events-auto">{empty}</div>
        </div>
      ) : null}
    </div>
  );
}

/** Every node in React Flow's store has a measured size (and there is one). */
function allMeasured(s: ReactFlowState): boolean {
  if (!s.nodeLookup.size) return false;
  for (const n of s.nodeLookup.values()) {
    if (!n.measured.width || !n.measured.height) return false;
  }
  return true;
}

function shallowSize(
  a: { width: number; height: number },
  b: { width: number; height: number },
): boolean {
  return a.width === b.width && a.height === b.height;
}

function nodeAria(n: StudioNode, issues: IssueMap, run: RunView | null): string {
  const kind = KINDS[n.data.spec.kind].label;
  const problems = issues.byNode.get(n.id)?.length ?? 0;
  const status = run?.nodes[n.id]?.status;
  return [
    `${kind} ${n.id}`,
    problems ? `${problems} problem${problems === 1 ? "" : "s"}` : "",
    status && status !== "pending" ? status.replace("_", " ") : "",
  ]
    .filter(Boolean)
    .join(", ");
}
