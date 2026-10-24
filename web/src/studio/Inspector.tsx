import { useId, useState, type ReactNode } from "react";
import { AlertIcon, TrashIcon } from "../components/icons";
import { toneChip } from "../components/severity";
import { fieldLabel, input, invalidInput, sectionTitle, subtleButton } from "./format";
import { isValidId, type StudioGraph } from "./graph";
import { KindIcon } from "./icons";
import { comingIn, KINDS, type Capabilities } from "./kinds";
import { ToolPicker } from "./ToolPicker";
import {
  BUDGET_NAMES,
  type Budgets,
  type Pattern,
  type SpecNode,
  type StudioRegistry,
} from "./types";
import { describeLoc, fieldIssues, type Issue, type IssueMap } from "./validate";
import { graphToSpec } from "./graph";
import { budgetLabel } from "./format";

function FieldIssues({ issues, id }: { issues: Issue[]; id?: string }) {
  if (!issues.length) return null;
  return (
    <ul id={id} className="mt-1 flex flex-col gap-0.5">
      {issues.map((i) => (
        <li
          key={`${i.source}:${i.msg}`}
          className="flex items-start gap-1 text-[11px] text-crit-fg"
        >
          <AlertIcon size={12} className="mt-px shrink-0" />
          <span>
            {i.msg}
            {i.source === "server" ? <span className="ml-1 opacity-75">(server)</span> : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Field({
  label,
  hint,
  issues = [],
  children,
}: {
  label: string;
  hint?: ReactNode;
  issues?: Issue[];
  children: (props: {
    id: string;
    "aria-invalid": boolean;
    "aria-describedby"?: string;
  }) => ReactNode;
}) {
  const id = useId();
  const errId = `${id}-errors`;
  return (
    <div>
      <label htmlFor={id} className={fieldLabel}>
        {label}
      </label>
      <div className="mt-1">
        {children({
          id,
          "aria-invalid": issues.length > 0,
          ...(issues.length ? { "aria-describedby": errId } : {}),
        })}
      </div>
      {hint ? <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">{hint}</p> : null}
      <FieldIssues issues={issues} id={errId} />
    </div>
  );
}

/** The node's id: edited as a draft, committed when valid and unique (edges follow). */
function IdField({
  value,
  taken,
  issues,
  disabled,
  onCommit,
}: {
  value: string;
  taken: string[];
  issues: Issue[];
  disabled: boolean;
  onCommit: (id: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const local: Issue[] =
    draft === value
      ? []
      : !isValidId(draft)
        ? [
            {
              loc: [],
              msg: "use lowercase letters, digits and _, starting with a letter (at most 31)",
              code: "invalid",
              source: "client",
            },
          ]
        : taken.includes(draft)
          ? [
              {
                loc: [],
                msg: `another node is already "${draft}"`,
                code: "invalid",
                source: "client",
              },
            ]
          : [];
  const commit = () => {
    if (draft !== value && local.length === 0) onCommit(draft);
  };
  return (
    <Field label="Id" issues={[...local, ...issues]} hint="Edges follow a renamed node.">
      {(props) => (
        <input
          {...props}
          value={draft}
          disabled={disabled}
          spellCheck={false}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") setDraft(value);
          }}
          className={`${input} font-mono ${local.length || issues.length ? invalidInput : ""}`}
        />
      )}
    </Field>
  );
}

interface NodeInspectorProps {
  graph: StudioGraph;
  node: SpecNode;
  issues: Issue[];
  registry: StudioRegistry | null;
  caps: Capabilities;
  /** False while a run is live: the graph being run stays as it is. */
  editable: boolean;
  onChange: (id: string, spec: SpecNode) => void;
  onRemove: (id: string) => void;
  onConnect: (from: string, to: string) => void;
  onDisconnect: (edgeId: string) => void;
}

/** Everything about one node: what it is told, what it may call, where it hands off. */
export function NodeInspector({
  graph,
  node,
  issues,
  registry,
  caps,
  editable,
  onChange,
  onRemove,
  onConnect,
  onDisconnect,
}: NodeInspectorProps) {
  const kind = KINDS[node.kind];
  const later = comingIn(node.kind, caps);
  const set = (patch: Partial<SpecNode>) => onChange(node.id, { ...node, ...patch });
  const others = graph.nodes.map((n) => n.id).filter((id) => id !== node.id);
  const outgoing = graph.edges.filter((e) => e.source === node.id && !e.data?.child);
  const incoming = graph.edges.filter((e) => e.target === node.id && !e.data?.child);
  const connectId = useId();
  const llm = node.kind === "llm_agent" || node.kind === "supervisor";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2.5">
        <span className="grid h-8 w-8 place-items-center rounded-lg bg-brand-50 text-brand-700 dark:bg-brand-950/70 dark:text-brand-200">
          <KindIcon kind={node.kind} size={17} />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="truncate font-mono text-sm font-semibold text-slate-900 dark:text-slate-100">
            {node.id}
          </h3>
          <p className="text-xs text-slate-500 dark:text-slate-400">{kind.label}</p>
        </div>
        <button
          type="button"
          className={`${subtleButton} hover:bg-crit-bg! hover:text-crit-fg!`}
          disabled={!editable}
          onClick={() => onRemove(node.id)}
        >
          <TrashIcon size={13} /> Remove
        </button>
      </div>

      {later ? (
        <p className={`rounded-lg border px-2.5 py-2 text-xs ${toneChip.refused}`}>
          {kind.label} nodes are {later}. This one is kept in the spec as it is, and the server will
          refuse to run it until then.
        </p>
      ) : null}
      <FieldIssues issues={fieldIssues(issues, "kind")} />

      <IdField
        key={node.id}
        value={node.id}
        taken={others}
        issues={fieldIssues(issues, "id")}
        disabled={!editable}
        onCommit={(id) => onChange(node.id, { ...node, id })}
      />

      <Field label="Description" issues={fieldIssues(issues, "description")}>
        {(props) => (
          <input
            {...props}
            value={node.description ?? ""}
            disabled={!editable}
            maxLength={500}
            placeholder="What this node is for (shown on the canvas)"
            onChange={(e) => set({ description: e.target.value })}
            className={input}
          />
        )}
      </Field>

      {llm ? (
        <Field
          label="Instruction"
          issues={fieldIssues(issues, "instruction")}
          hint={`${(node.instruction ?? "").length} / 8000 · the node's input is the previous node's answer (the first node gets the run's input).`}
        >
          {(props) => (
            <textarea
              {...props}
              rows={6}
              value={node.instruction ?? ""}
              disabled={!editable}
              placeholder="Tell the agent what to do with its input."
              onChange={(e) => set({ instruction: e.target.value })}
              className={`${input} min-h-28 resize-y font-mono text-[12.5px] leading-relaxed ${fieldIssues(issues, "instruction").length ? invalidInput : ""}`}
            />
          )}
        </Field>
      ) : null}

      {node.kind === "human_approval" ? (
        <Field label="Prompt" issues={fieldIssues(issues, "prompt")}>
          {(props) => (
            <input
              {...props}
              value={node.prompt ?? ""}
              disabled={!editable}
              onChange={(e) => set({ prompt: e.target.value })}
              className={input}
            />
          )}
        </Field>
      ) : null}

      {llm ? (
        <div>
          <p className={fieldLabel}>Tools</p>
          <div className="mt-1">
            <ToolPicker
              value={node.tools ?? []}
              onChange={(tools) => set({ tools })}
              registry={registry}
              issues={fieldIssues(issues, "tools")}
              disabled={!editable}
            />
          </div>
        </div>
      ) : null}

      {llm ? (
        <Field
          label="Model"
          issues={fieldIssues(issues, "model")}
          hint={
            caps.perNodeModels
              ? undefined
              : "Per-node models are coming in P0b; every node runs on the deployment's model."
          }
        >
          {(props) => (
            <select
              {...props}
              disabled={!caps.perNodeModels || !editable}
              value={node.model ?? ""}
              className={input}
              onChange={(e) => set({ model: e.target.value || null })}
            >
              <option value="">Deployment model</option>
              {node.model ? <option value={node.model}>{node.model}</option> : null}
            </select>
          )}
        </Field>
      ) : null}

      <div>
        <p className={fieldLabel}>Hands off to</p>
        <ul className="mt-1 flex flex-col gap-1">
          {incoming.map((e) => (
            <li
              key={e.id}
              className="flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-300"
            >
              <span className="text-slate-500 dark:text-slate-400">from</span>
              <span className="font-mono">{e.source}</span>
            </li>
          ))}
          {outgoing.map((e) => (
            <li
              key={e.id}
              className="flex items-center gap-1.5 text-xs text-slate-700 dark:text-slate-200"
            >
              <span className="text-slate-500 dark:text-slate-400">to</span>
              <span className="font-mono">{e.target}</span>
              {e.data?.spec.when ? (
                <span className="text-slate-500">when {e.data.spec.when}</span>
              ) : null}
              <button
                type="button"
                disabled={!editable}
                aria-label={`Remove the edge from ${e.source} to ${e.target}`}
                onClick={() => onDisconnect(e.id)}
                className={`${subtleButton} ml-auto`}
              >
                Unlink
              </button>
            </li>
          ))}
        </ul>
        {editable && others.length ? (
          <div className="mt-1.5 flex items-center gap-1.5">
            <label htmlFor={connectId} className="sr-only">
              Add an edge from {node.id} to
            </label>
            <select
              id={connectId}
              value=""
              onChange={(e) => e.target.value && onConnect(node.id, e.target.value)}
              className={`${input} py-1 text-xs`}
            >
              <option value="">Add an edge to…</option>
              {others
                .filter((id) => !outgoing.some((e) => e.target === id))
                .map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
            </select>
          </div>
        ) : null}
      </div>
    </div>
  );
}

const PATTERN_LABEL: Record<Pattern, string> = {
  pipeline: "Pipeline",
  fan_out_fan_in: "Fan-out / fan-in",
  router: "Router",
  supervisor: "Supervisor",
  critic_loop: "Critic loop",
  debate: "Debate",
  map_reduce: "Map-reduce",
  handoff: "Handoff",
  plan_and_execute: "Plan and execute",
};

/** With no node selected: the composition's own fields and every problem in it. */
export function CompositionInspector({
  graph,
  issues,
  caps,
  editable,
  onMeta,
  onSelectNode,
}: {
  graph: StudioGraph;
  issues: IssueMap;
  caps: Capabilities;
  editable: boolean;
  onMeta: (meta: StudioGraph["meta"]) => void;
  onSelectNode: (id: string) => void;
}) {
  const { meta } = graph;
  const specIssues = (field: string) =>
    issues.spec.filter((i) => (i.loc[0] === "budgets" ? i.loc[1] : i.loc[0]) === field);
  const setBudget = (name: keyof Budgets, raw: string) => {
    const n = raw === "" ? null : Number(raw);
    onMeta({
      ...meta,
      budgets: { ...(meta.budgets ?? {}), [name]: n !== null && Number.isFinite(n) ? n : null },
    });
  };
  const spec = graphToSpec(graph);
  const all: { issue: Issue; node: string | null }[] = [];
  for (const [node, list] of issues.byNode) for (const issue of list) all.push({ issue, node });
  for (const list of issues.byEdge.values())
    for (const issue of list) all.push({ issue, node: null });
  for (const issue of issues.spec) all.push({ issue, node: null });

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">Composition</h3>
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Select a node on the canvas to edit it. These fields belong to the whole run.
        </p>
      </div>
      <Field label="Name" issues={specIssues("name")} hint="Lowercase letters, digits, _ and -.">
        {(props) => (
          <input
            {...props}
            value={meta.name}
            disabled={!editable}
            spellCheck={false}
            onChange={(e) => onMeta({ ...meta, name: e.target.value })}
            className={`${input} font-mono ${specIssues("name").length ? invalidInput : ""}`}
          />
        )}
      </Field>
      <Field label="Description" issues={specIssues("description")}>
        {(props) => (
          <textarea
            {...props}
            rows={2}
            value={meta.description ?? ""}
            disabled={!editable}
            onChange={(e) => onMeta({ ...meta, description: e.target.value })}
            className={input}
          />
        )}
      </Field>
      <Field
        label="Pattern"
        issues={specIssues("pattern")}
        hint="A hint for templates and checks; the graph is what runs."
      >
        {(props) => (
          <select
            {...props}
            value={meta.pattern ?? "pipeline"}
            disabled={!editable}
            onChange={(e) => onMeta({ ...meta, pattern: e.target.value as Pattern })}
            className={input}
          >
            {(Object.keys(PATTERN_LABEL) as Pattern[]).map((p) => (
              <option key={p} value={p}>
                {PATTERN_LABEL[p]}
                {caps.patterns.has(p) ? "" : " (P0b)"}
              </option>
            ))}
          </select>
        )}
      </Field>
      <fieldset>
        <legend className={fieldLabel}>Budgets</legend>
        <p className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
          Empty: the deployment's ceiling. The first budget to run out ends the run.
        </p>
        <div className="mt-1.5 grid grid-cols-2 gap-2">
          {BUDGET_NAMES.map((name) => (
            <Field key={name} label={budgetLabel[name] ?? name} issues={specIssues(name)}>
              {(props) => (
                <input
                  {...props}
                  type="number"
                  min={1}
                  inputMode="numeric"
                  value={meta.budgets?.[name] ?? ""}
                  disabled={!editable}
                  placeholder="ceiling"
                  onChange={(e) => setBudget(name, e.target.value)}
                  className={`${input} font-mono tabular-nums`}
                />
              )}
            </Field>
          ))}
        </div>
      </fieldset>

      <section aria-labelledby="studio-problems">
        <h3 id="studio-problems" className={sectionTitle}>
          Problems {all.length ? `(${all.length})` : ""}
        </h3>
        {all.length === 0 ? (
          <p className="mt-1 text-xs text-ok-fg">
            No problems found. The server checks again when the run starts.
          </p>
        ) : (
          <ul className="mt-1.5 flex flex-col gap-1">
            {all.map(({ issue, node }) => (
              <li key={`${issue.source}:${JSON.stringify(issue.loc)}:${issue.msg}`}>
                <button
                  type="button"
                  disabled={node === null}
                  onClick={() => node && onSelectNode(node)}
                  className="w-full rounded-md px-2 py-1.5 text-left text-xs hover:bg-crit-bg focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:outline-none disabled:hover:bg-transparent"
                >
                  <span className="block font-mono text-[11px] text-slate-500 dark:text-slate-400">
                    {describeLoc(issue.loc, spec)}
                    {issue.source === "server" ? " · server" : ""}
                  </span>
                  <span className="text-crit-fg">{issue.msg}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
