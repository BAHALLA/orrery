# AEP-031: Orrery Studio — Composing Agents, A2A and MCP on a Canvas

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | High |
| **Impact** | High |
| **Dependencies** | [ADR-008](../adr/008-consuming-remote-a2a-agents.md) (remote A2A client), [AEP-027](aep-027-run-budgets.md) (run budgets), [AEP-009](aep-009-streaming.md) (SSE progress), [AEP-019](aep-019-web-console.md) (console) |

## Gap Analysis

Orrery has the parts of a composable agent runtime and none of the composition:

| Capability | Today | Missing for composition |
|------------|-------|-------------------------|
| Patterns | Sequential, parallel + `JoinNode`, router, critic loop and agent-as-tool exist in ADK 2.12 and in the triage `Workflow` | Hand-written Python only. `AgentGateway` accepts a `Workflow` root, but nothing builds one from data |
| Runtime editing | `PUT /admin/catalog`: declarative agents (instruction + catalogue tools) and on/off switches | No topology, no toolsets or nested agents, and a save replaces the **live** tree for everyone |
| Models | One process-wide `MODEL_PROVIDER`/`MODEL_NAME` wrapped in `FallbackLlm` | No per-agent model, so no "same pattern, two models" comparison |
| MCP | Hardened client (`GuardedMcpToolset`), three transports | Servers fixed in `ORRERY_MCP_CONFIG` at startup |
| A2A | Inbound only ([ADR-007](../adr/007-a2a-exposition.md)) | No way to call a remote agent |
| Budgets | AEP-027 counts tool calls and delegations per agent run | No token or wall-clock budget for a whole multi-agent run |
| Trace | `ProgressStep` tree over SSE (`/chat/stream`) | No node enter/exit, route decisions, per-node tokens, or A2A message timeline |
| Console | Chat, `RunTrace` chips, Agents form | No canvas, no draft, no run history |

ADK's own declarative format (`AgentConfig`) is not the answer. It is
deprecated in 2.12, and it resolves models, tools and callbacks through
`CodeConfig` **Python import paths**: an API that accepts it accepts arbitrary
imports.

The goal is a sandbox where an admin builds a composition (local agents,
remote A2A agents, MCP servers, wired in a named pattern), runs it apart from
the live coordinator, and watches it execute. That serves three jobs the
product cannot do today: demonstrating A2A and MCP, testing third-party agents
and servers safely, and comparing patterns.

## Proposed Solution

### Non-goals (v1)

- Public or anonymous use. v1 is **admin only**, and human admins only (below).
- stdio MCP servers in compositions (that is running someone else's code on
  the host). HTTP transports only.
- Changing the live coordinator. Studio runs never call `replace_root_agent()`
  or write `/admin/catalog`.
- Arbitrary egress. Every remote endpoint comes from a deployment-config
  registry.

### What "sandbox" does and does not mean

A Studio run is **separate from**, not **isolated from**, the live system:

- It never changes the live agent tree, the catalog or chat sessions.
- Its catalogue tools are the real ones. A `kafka_*` or `kubectl_*` tool in a
  composition reads the real cluster. Studio runs are therefore capped at **L2**
  (reads only) for catalogue tools, whatever the deployment's ceiling.
- Plugins are instantiated fresh per run (`build_plugins()`), so rate-limit,
  delegation and budget state never mixes with chat traffic.

Whether the Studio runs in the main deployment behind a flag, or as its own
deployment with its own catalogue (recommended for the public demo), is a P1
decision. P0a runs in-process behind `ORRERY_STUDIO_ENABLED`.

### 1. The composition spec

A versioned JSON document, validated by a Pydantic model in
`orrery_core/studio/spec.py` and published as JSON Schema for the console:

```json
{
  "spec_version": 1,
  "name": "release-notes-review",
  "pattern": "critic_loop",
  "budgets": { "max_tokens": 200000, "max_tool_calls": 40, "timeout_s": 180 },
  "nodes": [
    { "id": "research", "kind": "llm_agent", "model": "anthropic/claude-haiku-5-5",
      "instruction": "Collect the changes...", "tools": ["mcp:tirraflow.search_articles"] },
    { "id": "writer", "kind": "llm_agent",
      "instruction": "Ask the writer agent for a draft from the research notes.",
      "tools": ["a2a:demo-writer.draft"] },
    { "id": "critic", "kind": "llm_agent", "model": "openai/gpt-5-mini",
      "instruction": "Reject unless every claim cites a source.",
      "output": { "labels": ["approved", "rejected"] } },
    { "id": "gate", "kind": "human_approval", "prompt": "Publish this draft?" }
  ],
  "edges": [
    { "from": "research", "to": "writer" },
    { "from": "writer", "to": "critic" },
    { "from": "critic", "to": "writer", "when": "rejected", "max_iterations": 3 },
    { "from": "critic", "to": "gate", "when": "approved" }
  ]
}
```

| `kind` | Compiles to |
|--------|-------------|
| `llm_agent` | `create_agent()` (so every guard, plugin and confirmation hook applies), with the node's model if allowed |
| `remote_a2a` | Console shorthand for an `llm_agent` that holds one `GuardedRemoteA2aTool` ([ADR-008](../adr/008-consuming-remote-a2a-agents.md)) and an instruction to forward the task. **Never a bare `RemoteA2aAgent` node**: that would skip every tool-level guard and send the whole session to the remote |
| MCP server | Not a node. A `GuardedMcpToolset` built per run from a registry entry, filtered to the tools a node references as `mcp:<entry>.<tool>`, closed when the run ends |
| `router` | A `FunctionNode` that reads the previous node's `output_schema` label and sets `ctx.route`. An edge with `when` requires its source to declare `output.labels` |
| `parallel` / `join` | Node tuple + `JoinNode` |
| `loop` | An edge back with `max_iterations`, compiled to a counter `FunctionNode` on that edge that stops the loop at the cap (as `verify_route` does) |
| `supervisor` | An `llm_agent` whose children are wrapped as `AgentTool`s. Children must be `llm_agent`s: ADK's `AgentTool` takes a `BaseAgent`, not a graph |
| `human_approval` | A new Orrery node backed by the existing confirmation store (below) |

`pattern` is a hint for the console's templates and for the compiler's checks
(a `router` pattern must have labelled edges); the graph is what runs.

**Guardrails are not a node.** Screening and PII redaction run at every remote
A2A and MCP boundary, always, because they are tool results. An optional
`screen: true` on an edge adds the same screen between two `llm_agent` nodes.

**Compiler details.**

- Spec ids are prefixed (`n_<id>`) so they cannot collide with ADK's reserved
  names or the catalogue's agent names.
- Each run gets its own toolsets and closes them in `finally`.
- `spec_version` is checked on load; an older version is upgraded by an
  explicit function, and a newer one is refused.

**Models.** A node's `model` must be in `ORRERY_STUDIO_MODELS`. A node with a
model runs that model only, with no `FallbackLlm` chain, so a comparison is
honest; a node without one uses the deployment's model and its fallbacks.
`resolve_model()` gains an optional argument for this.

**Validation, before any model call:**

- at most 30 nodes and 6 parallel branches;
- supervisor nesting at most 3 levels deep;
- `max_iterations` ≤ 5, and no cycle without one;
- a reachable terminal node;
- tools only from the catalogue (read-only) or the registry;
- budgets within the deployment's ceilings.

Errors are field-level, so the console can point at the node.

### 2. Runs

Each run gets its own `AgentGateway` with `app_name="orrery-studio"`,
`memory_service=None`, `verified_confirmation=True` and a fresh plugin list.
That is the same plugin stack chat uses, with no second runner to keep in step.
`AgentGateway` already accepts a `Workflow` root.

- `POST /studio/runs` with `{spec, input}` (P0a) or `{composition_id, input}`
  (P0b). It compiles, validates and starts the run, then returns `{run_id}`.
- `GET /studio/runs/{id}/events` (SSE, with `Last-Event-ID` resume) and
  `GET /studio/runs/{id}`.
- `POST /studio/runs/{id}/cancel`. This cancels the run's task: in-flight model
  and HTTP calls are cancelled, toolsets are closed, and the run ends as
  `cancelled`. A remote A2A task is sent `tasks/cancel` (best effort).
- `POST /studio/runs/{id}/approvals/{approval_id}` resumes a run paused at a
  `human_approval` node or by a remote `input-required` (P0c).

**Sessions and memory.** Sessions use the `s-` prefix, which is added to
`is_machine_session()`. They are never written to long-term memory and never
appear in chat history.

**Budgets.** AEP-027 counts per agent run. Studio adds a **composition ledger**
for the whole run: tokens (from `usage_metadata`), tool calls, delegations,
remote calls and a deadline. Whichever runs out first ends the run with a
`budget.hit` event. `ORRERY_DAILY_TOKEN_BUDGET_PER_USER` still applies.

**Replicas.** In P0a a run lives on the replica that started it. The SSE and
cancel endpoints return 404 elsewhere, and the console uses sticky sessions.
From P0b, events go to Postgres so any replica can serve them. Cancel stays
local until there is a shared signal (Postgres `LISTEN/NOTIFY`).

**Storage (P0b).** Compositions are versioned documents
(`studio_compositions`, optimistic concurrency like `catalog_store`). Runs and
events are kept for `ORRERY_STUDIO_RUN_RETENTION_DAYS` (default 14). Orrery
has no migration tool (`create_all` only), so these are new tables only, never
changes to existing ones.

### 3. Human approval

`human_approval` is not ADK's `FunctionNode(require_confirmation=True)`. That
raises a `RequestInput` interrupt, which the model-facing caller could answer
on its own. Instead, the node:

- writes a pending entry to the existing confirmation store with the run,
  node and a digest of the payload;
- pauses the run and emits `approval.requested`;
- resumes only on the approval endpoint, called by a human admin, and checks
  with `consume_approved()`, like the requester-verified gate.

A remote agent's `input-required` uses the same path: a human answers it, never
a model.

### 4. Trace events v2

Superset of `ProgressStep`, on the studio SSE stream only:

| Event | Fields |
|-------|--------|
| `run.start` / `run.end` | spec digest, outcome, totals (tokens in/out per model, tool calls, wall time) |
| `node.enter` / `node.exit` | `node_id`, `kind`, `model`, tokens, `elapsed_ms`, outcome |
| `route.decision` | `node_id`, chosen label, alternatives |
| `loop.iteration` | edge, iteration, cap |
| `a2a.message` | remote, direction, method, task id, task state transitions, part kinds and sizes |
| `mcp.call` | entry, tool, classification, outcome, `elapsed_ms` |
| `approval.requested` / `approval.decided` | as AEP-024 |
| `budget.hit` | which budget |

- Arguments, results and message text stay off the wire by default, as in
  `ProgressPlugin`. With `ORRERY_STUDIO_CAPTURE_CONTENT=true`, text goes out
  only **after** PII redaction, and is cut to 2 KB per event.
- At most 2000 events per run; past that, `node.*` events are sampled and
  `run.end` says how many were dropped.

A run's events are what the console renders, replays and compares.

### 5. The endpoint registry

A YAML file named by `ORRERY_STUDIO_REGISTRY`, read at startup (deployment
config, like `ORRERY_MCP_CONFIG`). Editing it from the console is P0c.

```yaml
mcp:
  - id: tirraflow
    url: https://tirraflow.com/mcp
    transport: streamable_http
    tools: { search_articles: read_only, get_article: read_only, list_series: read_only }
a2a:
  - id: demo-writer
    card_url: https://demo-writer.studio.internal/.well-known/agent-card.json
    card_sha256: "…"           # JCS digest of the trust fields (ADR-008)
    skills: [draft]
    effect: read_only           # acting -> @destructive: L4 + confirmation
    private_network: true       # host must match ORRERY_A2A_PRIVATE_ALLOW
    auth: { bearer_env: ORRERY_STUDIO_SECRET_DEMO_WRITER }
```

- Every entry goes through the network guard (ADR-008): public addresses
  unless `private_network` is set **and** the host is in
  `ORRERY_A2A_PRIVATE_ALLOW`; metadata and link-local always blocked; no
  redirects; byte caps.
- Secrets come only from env vars named `ORRERY_STUDIO_SECRET_*`. Each is sent
  only to its entry's origin. No file paths, no stored values.
- When editing arrives (P0c), it is human admins only, and every write is
  audited with the before and after.

### 6. The Studio console

A `studio` view in `web/` (React 19, TypeScript, Tailwind 4), with
[`@xyflow/react`](https://reactflow.dev) for the canvas:

- **Canvas:** palette of node kinds, pattern templates (pipeline, fan-out/fan-in,
  router, supervisor, critic loop, debate, map-reduce, handoff, plan-and-execute),
  per-node inspector, live validation against the published JSON Schema.
- **Run panel:** the graph animates from trace events (active node, route taken,
  loop counter, tokens and latency per node); a timeline view of the same events.
- **A2A inspector:** a registry card's skills and capabilities, its pinned
  digest, and a message/task-state timeline for each remote call.
- **MCP inspector:** a registry server's tools, schemas and classification,
  and a try-it call (read-only tools only).
- **History and compare:** past runs of a composition, replay, and two runs side
  by side (pattern A vs B, model A vs B) with totals.
- **Export:** the spec as JSON/YAML, and generated ADK Python for the
  composition. It is read-only output that the server never executes, and every
  string from the spec is emitted as an escaped literal, never interpolated
  into code.

**Access.** `/studio/*` requires `require_admin` **and** a human principal (a
client listed in `ORRERY_HUMAN_CLIENTS`). Service tokens with an admin role are
refused.

**CSP.** React Flow sets inline `style` attributes. The console's CSP must
allow them (`style-src 'unsafe-inline'`, or nonce-based styles) before the
canvas ships. Check this against the deployed nginx policy in P1.

### 7. The demo fleet

A compose profile `studio-demo` with small images on an internal network, so
A2A across frameworks can be shown without opening egress:

- a plain `a2a-sdk` agent and a LangGraph agent: different frameworks, same
  protocol;
- each served over **https** with an internal CA, since ADK refuses plain
  http cards off loopback;
- MCP: Tirraflow's own public `/mcp`, plus one small local server (time/units).

Orrery's own A2A adapter is **not** in the fleet until P2. It refuses any
request carrying `x-orrery-hop`, which every outbound call sets
(ADR-007/ADR-008). Orrery calling Orrery needs the hop header turned into a
counter, and an amendment to ADR-007.

### Phases

1. **P0a, one pattern end to end (API only):**
   - the YAML registry (MCP and A2A, read-only remotes);
   - `llm_agent` nodes with `mcp:` and `a2a:` tool references, and a sequential
     pipeline only;
   - `GuardedRemoteA2aTool`, and the httpx transport factored out of the MCP
     guard (ADR-008);
   - an `AgentGateway` per run at L2, in memory;
   - `POST /studio/runs` and its SSE stream with `run.*`, `node.*`,
     `a2a.message` and `mcp.call` events;
   - the composition ledger;
   - the demo: research (LLM + Tirraflow MCP) → writer (plain `a2a-sdk` agent
     over https) → critic (LLM), as an end-to-end test in CI with a stub
     model.
2. **P0b, patterns and storage:**
   - `router`, `loop`, `parallel`/`join` and `supervisor`;
   - per-node models;
   - Postgres compositions, runs and events, and replay;
   - multi-replica event reads.
3. **P0c, people in the loop:**
   - `human_approval` and remote `input-required`;
   - `effect: acting` remotes (L4 + confirmation);
   - registry editing with an audit trail.
4. **P1, Studio v1 (admin only):** canvas, templates, run panel, A2A and MCP
   inspectors, history and compare, the LangGraph agent in the demo fleet, the
   CSP check, and the own-deployment decision.
5. **P2, frontier features:**
   - **Publish as A2A:** a saved composition gets its own agent card and is
     served by the ADR-007 adapter, with the hop counter, so compositions can
     call compositions.
   - **Eval sets:** run a composition over a dataset with an LLM judge
     (AEP-002 harness), and compare patterns on scores, cost and latency.
   - Share a run as a read-only replay link.
6. **P3, others' hands:** invited or public users, per-learner isolation on a
   separate runner host (Tirraflow TEP-007), bring-your-own endpoints.

### Affected Files

- New:
  - `core/orrery_core/studio/{spec,compiler,runner,ledger,events,registry,approval}.py`;
  - `core/orrery_core/a2a/remote.py` (ADR-008);
  - `store.py` and the `studio_*` tables (P0b).
- `core/orrery_core/tools/mcp_network.py`: the library-neutral check and the
  httpx transport (ADR-008).
- `core/orrery_core/serving/server.py`: mount `/studio/*` when
  `ORRERY_STUDIO_ENABLED=true`. The session helpers also add `s-` to
  `is_machine_session()`.
- `core/orrery_core/agent/base.py`: `resolve_model(model=None)` accepts a
  per-node model from the allowlist (P0b).
- `web/`: `src/studio/*` and the `@xyflow/react` dependency (P1).
- `deploy/helm/orrery-assistant/`: studio values. `docker-compose.yml`: the
  `studio-demo` profile.
- Docs:
  - `docs/integrations/studio.md`;
  - `docs/integrations/a2a.md` (outbound section);
  - this AEP and ADR-008.

## Security

| Risk | Control |
|------|---------|
| Arbitrary imports through a spec | The spec references catalogue tools and registry entries by id only; no import paths, no code fields |
| SSRF through MCP or A2A URLs | Registry in deployment config; network guard on both httpx stacks; private addresses only for allowlisted hosts or CIDRs |
| A remote agent seeing our data | Remote agents are tools that receive only the model's request, never the session (ADR-008) |
| A remote agent acting on our behalf | `effect: acting` is `@destructive`: L4 + requester-verified confirmation; unavailable at the L2 default |
| Prompt injection travelling between nodes | Every remote reply and MCP result is a tool result: screened, PII-redacted and capped; optional `screen` on LLM→LLM edges |
| Studio runs touching real systems | Catalogue tools capped at L2 in studio runs; fresh plugin state per run |
| Loops between agents | Hop header for direct Orrery→Orrery calls; composition ledger (delegations, remote calls, deadline) for the rest |
| Cost | Composition ledger, model allowlist, `ORRERY_DAILY_TOKEN_BUDGET_PER_USER` |
| Leaking content into traces | Arguments, results and message text off by default; when captured, redacted first and cut to size |
| Machine callers driving the Studio | Human admins only (`ORRERY_HUMAN_CLIENTS`) |
| Changing production behaviour | Runs never replace the live tree or the catalog (test) |

## Acceptance Criteria

**P0a**

- [ ] The demo pipeline runs end to end in CI (stub model, real `a2a-sdk` agent over https, local MCP server). It emits `run.*`, `node.*`, `a2a.message` and `mcp.call` events.
- [ ] `GuardedRemoteA2aTool` refuses (tests):
  - unregistered remotes;
  - a changed card digest;
  - RPC targets off the allowed origins;
  - redirects;
  - private and metadata addresses (unless allowlisted);
  - oversized cards and replies.
- [ ] The remote agent receives only the tool request: a canary in an earlier tool result never reaches it (test).
- [ ] A remote reply passes through the safety screen and PII redaction (test with an injection string and an email address).
- [ ] Invalid specs are refused with a field-level error before any model call.
- [ ] A studio run never changes `/admin/catalog`, the chat root or memory (test).
- [ ] A service token with an admin role gets 403 on `/studio/runs`.

**P0b and later**

- [ ] Every node kind compiles and runs; a recorded run replays to the same graph state.
- [ ] A loop stops at its cap, and a ledger budget ends the run with `budget.hit`.
- [ ] An approval can only be resumed by a human admin, once (P0c).
- [ ] The console builds every pattern template, runs it and renders the live graph; axe-clean in both themes (P1).

## Open Questions

- Own deployment for the Studio (own catalogue, host and budgets) or a flag on the main one? Decided in P1.
- Cost estimates need AEP-015's price table. Until then, show tokens only?
- Should "publish as A2A" compositions be callable by the main coordinator as specialists?
