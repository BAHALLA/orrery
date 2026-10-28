import { useMemo, type DragEvent } from "react";
import { toneChip } from "../components/severity";
import { KIND_MIME } from "./Canvas";
import { sectionTitle } from "./format";
import { layout } from "./graph";
import { KindIcon } from "./icons";
import { canAdd, comingIn, KINDS, PALETTE_KINDS, type Capabilities } from "./kinds";
import { templateNeeds, type Template } from "./templates";
import type { CompositionSpec, NodeKind } from "./types";

/**
 * The palette of node kinds. Drag one onto the canvas, or press it (mouse or
 * keyboard) to append it to the end of the graph. Kinds this server cannot
 * run stay listed, focusable and labelled with the phase that brings them.
 */
export function Palette({
  caps,
  editable,
  onAdd,
}: {
  caps: Capabilities;
  editable: boolean;
  onAdd: (kind: NodeKind) => void;
}) {
  return (
    <section aria-labelledby="studio-palette">
      {/* Below @6xl (the Studio's own width) the palette is an icon rail: the text stays for screen readers. */}
      <h3
        id="studio-palette"
        className={`${sectionTitle} sr-only px-1 pb-2 @6xl/studio:not-sr-only`}
      >
        Nodes
      </h3>
      <ul className="flex flex-col gap-1">
        {PALETTE_KINDS.map((kind) => {
          const info = KINDS[kind];
          const later = comingIn(kind, caps);
          const usable = editable && canAdd(kind, caps);
          const onDragStart = (e: DragEvent) => {
            e.dataTransfer.setData(KIND_MIME, kind);
            e.dataTransfer.effectAllowed = "copy";
          };
          return (
            <li key={kind}>
              <button
                type="button"
                draggable={usable}
                onDragStart={usable ? onDragStart : undefined}
                aria-disabled={!usable}
                onClick={() => usable && onAdd(kind)}
                title={
                  later
                    ? `${info.label}: ${later}`
                    : `${info.label}: ${info.blurb} Drag onto the canvas, or press to add at the end.`
                }
                className={`group flex w-full items-center justify-center gap-2.5 rounded-lg border px-1.5 py-2 text-left @6xl/studio:items-start @6xl/studio:justify-start @6xl/studio:px-2.5 transition-colors focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none ${
                  usable
                    ? "cursor-grab border-slate-200 bg-white hover:border-brand-300 hover:bg-brand-50/50 active:cursor-grabbing dark:border-slate-800 dark:bg-slate-900 dark:hover:border-brand-800 dark:hover:bg-brand-950/30"
                    : "cursor-not-allowed border-dashed border-slate-200 bg-transparent dark:border-slate-800"
                }`}
              >
                <span
                  className={`grid h-6 w-6 shrink-0 place-items-center rounded-md @6xl/studio:mt-0.5 ${
                    usable
                      ? "bg-brand-50 text-brand-700 dark:bg-brand-950/70 dark:text-brand-200"
                      : "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400"
                  }`}
                >
                  <KindIcon kind={kind} size={14} />
                </span>
                <span className="sr-only min-w-0 flex-1 @6xl/studio:not-sr-only">
                  <span
                    className={`flex items-center gap-1.5 text-sm font-medium ${
                      usable
                        ? "text-slate-800 dark:text-slate-100"
                        : "text-slate-600 dark:text-slate-400"
                    }`}
                  >
                    {info.label}
                    {later ? (
                      <span
                        className={`rounded px-1 py-px text-[10px] font-semibold ${toneChip.refused}`}
                      >
                        {later}
                      </span>
                    ) : null}
                  </span>
                  <span className="block text-[11px] leading-snug text-slate-500 dark:text-slate-400">
                    {info.blurb}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** A thumbnail of a spec's shape, from the same layout the canvas uses. */
export function SpecThumbnail({ spec, muted }: { spec: CompositionSpec; muted: boolean }) {
  const shape = useMemo(() => {
    const edges = [
      ...spec.edges,
      ...spec.nodes.flatMap((n) => (n.children ?? []).map((to) => ({ from: n.id, to }))),
    ];
    const pos = layout(
      spec.nodes.map((n) => n.id),
      edges,
    );
    const xs = Object.values(pos).map((p) => p.x);
    const ys = Object.values(pos).map((p) => p.y);
    const [minX, maxX, minY, maxY] = [
      Math.min(...xs),
      Math.max(...xs),
      Math.min(...ys),
      Math.max(...ys),
    ];
    const W = 132;
    const H = 52;
    const sx = maxX > minX ? (W - 28) / (maxX - minX) : 0;
    const sy = maxY > minY ? (H - 16) / (maxY - minY) : 0;
    const at = (id: string) => ({
      x: 14 + ((pos[id]?.x ?? 0) - minX) * sx + (maxX === minX ? (W - 28) / 2 : 0),
      y: 8 + ((pos[id]?.y ?? 0) - minY) * sy + (maxY === minY ? (H - 16) / 2 : 0),
    });
    return { W, H, at, edges };
  }, [spec]);

  const stroke = muted
    ? "stroke-slate-300 dark:stroke-slate-600"
    : "stroke-brand-300 dark:stroke-brand-700";
  return (
    <svg viewBox={`0 0 ${shape.W} ${shape.H}`} className="h-12 w-full" aria-hidden="true">
      {shape.edges.map((e) => {
        const a = shape.at(e.from);
        const b = shape.at(e.to);
        const back = b.x <= a.x;
        return (
          <path
            key={`${e.from}-${e.to}`}
            d={
              back
                ? `M${a.x},${a.y} C${a.x + 10},${a.y + 22} ${b.x - 10},${b.y + 22} ${b.x},${b.y}`
                : `M${a.x},${a.y} C${(a.x + b.x) / 2},${a.y} ${(a.x + b.x) / 2},${b.y} ${b.x},${b.y}`
            }
            className={`fill-none ${stroke}`}
            strokeWidth={1.4}
            strokeDasharray={back ? "2 2" : undefined}
          />
        );
      })}
      {spec.nodes.map((n) => {
        const p = shape.at(n.id);
        const llm = n.kind === "llm_agent" || n.kind === "supervisor";
        return (
          <rect
            key={n.id}
            x={p.x - (llm ? 7 : 4.5)}
            y={p.y - (llm ? 4.5 : 4.5)}
            width={llm ? 14 : 9}
            height={9}
            rx={llm ? 2.5 : 4.5}
            className={
              muted
                ? "fill-slate-200 dark:fill-slate-700"
                : llm
                  ? "fill-brand-500 dark:fill-brand-400"
                  : "fill-brand-200 dark:fill-brand-800"
            }
          />
        );
      })}
    </svg>
  );
}

/** Pattern templates. Those this server cannot run yet are shown, marked, and not loadable. */
export function TemplateGallery({
  templates,
  caps,
  onPick,
  compact = false,
}: {
  templates: readonly Template[];
  caps: Capabilities;
  onPick: (template: Template) => void;
  compact?: boolean;
}) {
  return (
    <ul
      className={`grid gap-2 ${compact ? "grid-cols-2" : "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3"}`}
    >
      {templates.map((t) => {
        const needs = templateNeeds(t, caps);
        return (
          <li key={t.id}>
            <button
              type="button"
              aria-disabled={needs !== null}
              onClick={() => needs === null && onPick(t)}
              title={needs ? `${t.title}: needs ${needs}` : `Load the ${t.title} template`}
              className={`flex h-full w-full flex-col gap-1.5 rounded-xl border p-2.5 text-left transition focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none ${
                needs
                  ? "cursor-not-allowed border-dashed border-slate-200 dark:border-slate-800"
                  : "border-slate-200 bg-white hover:-translate-y-px hover:border-brand-300 hover:shadow-md dark:border-slate-800 dark:bg-slate-900 dark:hover:border-brand-800"
              }`}
            >
              <SpecThumbnail spec={t.spec} muted={needs !== null} />
              <span className="flex items-center gap-1.5">
                <span
                  className={`text-sm font-medium ${needs ? "text-slate-600 dark:text-slate-400" : "text-slate-900 dark:text-slate-100"}`}
                >
                  {t.title}
                </span>
                {needs ? (
                  <span
                    className={`ml-auto shrink-0 rounded px-1 py-px text-[10px] font-semibold ${toneChip.refused}`}
                  >
                    needs {needs}
                  </span>
                ) : null}
              </span>
              {compact ? null : (
                <span className="text-xs leading-snug text-slate-500 dark:text-slate-400">
                  {t.blurb}
                </span>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
