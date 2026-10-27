# AEP-031: Orrery Studio — Composing Agents, A2A and MCP on a Canvas

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">in-progress</span> (P0a built: API; P1 console in review, see [Studio](../integrations/studio.md)) |
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
| Models | One process-wide `MODEL_PROVIDER`/`MODEL_NAME` (wrapped in `FallbackLlm` when `MODEL_FALLBACK_CHAIN` is set) | No per-agent model, so no "same pattern, two models" comparison |
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
  (reads only), whatever the deployment's ceiling. `AutonomyPlugin` has one
  level per session, so in P0a/P0b the whole run is L2. Acting remotes (P0c)
  need a per-tool rule.
- Plugins are instantiated fresh per run (`default_plugins(autonomy_level="L2")`),
  so delegation, repeat and circuit-breaker state never mixes with chat traffic.
  The one exception is the daily token ledger: the per-run `TokenBudgetPlugin`
  gets the process's shared ledger, otherwise every run would start at zero.
- The default thread-pool executor is still shared with chat; a separate
  executor for studio tools is a P1 item.

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
`session_service=InMemorySessionService()` (explicitly, so no durable store is
ever picked up), `memory_service=None`, `verified_confirmation=True` and a fresh plugin list.
That is the same plugin stack chat uses, with no second runner to keep in step.
`AgentGateway` already accepts a `Workflow` root.

- `POST /studio/runs` with `{spec, input}` (P0a) or `{composition_id, input}`
  (P0b). It compiles, validates and starts the run, then returns `{run_id}`.
- `GET /studio/runs/{id}/events` (SSE, with `Last-Event-ID` resume) and
  `GET /studio/runs/{id}`.
- `POST /studio/runs/{id}/cancel`. This cancels the run's task: in-flight model
  and HTTP calls are cancelled, toolsets are closed, and the run ends as
  `cancelled`. A sync tool already running in a worker thread cannot be
  interrupted and finishes in the background (harmless at L2). A remote A2A
  task is not sent `tasks/cancel` in P0a; best-effort `tasks/cancel` is a later
  item.
- `POST /studio/runs/{id}/approvals/{approval_id}` resumes a run paused at a
  `human_approval` node or by a remote `input-required` (P0c).

**Sessions and memory.** Sessions use the `s-` prefix, which is added to
`is_machine_session()`. They are never written to long-term memory and never
appear in chat history.

**Budgets.** AEP-027 counts per agent run. Studio adds a **composition ledger**
for the whole run: tokens (from `usage_metadata`), tool calls, delegations,
remote calls and a deadline. Whichever runs out first ends the run with a
`budget.hit` event. `ORRERY_DAILY_TOKEN_BUDGET_PER_USER` still applies, through
the shared ledger (above).

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
- pauses the run and emits `approval.requested`. Mechanically the node yields a
  `RequestInput`, but the client can never answer it: `/chat` and `/studio`
  refuse client-supplied resumes for studio sessions, and only the server
  injects the resume after `consume_approved()`;
- resumes only on the approval endpoint, called by a human admin, and checks
  with `consume_approved()`, like the requester-verified gate.

A remote agent's `input-required` uses the same path: a human answers it, never
a model.

*As built (P0c), the mechanism is simpler than the plan above:* see
[P0c as built](#p0c-as-built). The guarantees (only the owner, once, against a
digest of what was read) are the same.

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
- At most 2000 events per run; past that, events are dropped (a few slots
  stay reserved for `budget.hit` and `run.end`), and `run.end` says how many
  were. P0a drops rather than samples: a sampled trace would show nodes that
  never exited.

A run's events are what the console renders, replays and compares.

### 5. The endpoint registry

A YAML file named by `ORRERY_STUDIO_REGISTRY`, read at startup (deployment
config, like `ORRERY_MCP_CONFIG`). Editing it from the console is P0c.

```yaml
mcp:
  - id: tirraflow
    url: https://tirraflow.com/mcp
    transport: streamable_http
    tools: [search_articles, get_article, list_series]
    read_only: [search_articles, get_article, list_series]
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
refused. With `ORRERY_HUMAN_CLIENTS` unset every console token counts as human,
so the Studio refuses to start without it.

**CSP.** The console's CSP comes from `serving/security_headers.py`. React and
React Flow set styles through the CSSOM, which `style-src 'self'` allows, so
nothing is loosened in advance: the canvas is tested under the real policy in
P1, and any deployment-side nginx policy is checked then too.

### 7. The demo fleet

A compose profile `studio-demo` with small images on an internal network, so
A2A across frameworks can be shown without opening egress:

- a plain `a2a-sdk` agent and a LangGraph agent: different frameworks, same
  protocol;
- each served over **https** with an internal CA (given to Orrery as
  `ORRERY_A2A_CA_FILE`), since ADK refuses plain http cards off loopback. CI
  tests use plain http on loopback instead, which ADK and the guard allow;
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
     over https in the compose demo) → critic (LLM), and the same pipeline as
     an end-to-end test in CI with a stub model and loopback http;
   - `ORRERY_STUDIO_ENABLED` refuses to start without `ORRERY_HUMAN_CLIENTS`.
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
  - `core/orrery_core/studio/{spec,compiler,runner,ledger,events,registry,config,api}.py`
    (P0a), and `approval.py` (P0c);
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

### P0a as built

Where the code differs from, or pins down, the design above:

- **Tool references** add `catalog:<name>` for the deployment's own catalogue
  tools (`studio_catalogue` in `create_app`, read-only ones only). Model-facing
  names are `mcp_<entry>_<tool>` and `a2a_<entry>_<skill>`. Validation computes
  each node's model-facing names and refuses two that collide
  (`tool_name_collision`: `-` and `_` map to one name; a catalogue alias may
  match another tool) and MCP names over 64 characters (`tool_name_too_long`),
  so a spec that passes cannot fail when ADK registers its tools.
- **Every node kind is in the format** (`remote_a2a`, `router`, `parallel`,
  `join`, `supervisor`, `human_approval`, plus edge `when`, `max_iterations`
  and `screen`, and node `model` and `output`), so specs written for later
  phases parse. P0a refuses them with a `not_supported_yet` error at the field.
  Unknown keys are refused everywhere.
- **Plugins** are `default_plugins(enable_auth=True, autonomy_level="L2",
  enable_memory=False)` per run, plus a `StudioRunPlugin` (trace events and
  the ledger) placed before `ToolOutputCapPlugin`, which answers an oversized
  result with a replacement and so ends the after-tool chain: a capped MCP
  result still emits its `mcp.call`. A `StudioSpentGate` sits before
  `TokenBudgetPlugin`, so a call made once the run's budget is spent is
  answered without reserving tokens it would never release. `default_plugins()`
  gained a `token_ledger` argument; by default every call shares one
  per-process ledger per storage URI (`shared_usage_ledger()`).
- **MCP tool pins live per process**, in a store per registry entry
  (`McpPins`), not per run, so a tool description changed between runs is
  caught as it is for `ORRERY_MCP_CONFIG` servers. Studio MCP entries trust the
  same `ORRERY_A2A_CA_FILE` as A2A entries, through one SSL context per
  network built once.
- **Attribution by node.** `mcp.call` events key MCP tools by node and name,
  so two nodes using the same tool are told apart.
- **A run cancelled before its task first runs** still ends: a done-callback
  emits `run.end` (`cancelled`) and the audit record. A cancelled run's task
  calls `uncancel()` once it has cleaned up, so its final events go out.
- **Memory bounds.** Besides `ORRERY_STUDIO_MAX_RUNS` and retention, a person
  keeps at most `ORRERY_STUDIO_MAX_RUNS_PER_USER` finished runs, and a run's
  captured content is capped at `ORRERY_STUDIO_MAX_CONTENT_BYTES_PER_RUN`.
- **Captured content** honours `ORRERY_REDACT_IPS`. A remote reply's content
  is taken after the safety screen: its inbound `a2a.message` is held until the
  tool call returns, then sent with the screened reply, before the node exits.
  Held events are keyed by the function-call id, so parallel calls from one
  node keep their own replies.
- **A tool call past a budget** is refused before it runs with the existing
  `CALL_BUDGET_EXHAUSTED` status, which the circuit breaker already treats as
  neutral, and the run then ends.
- **A failed model call fails the run.** `ErrorHandlerPlugin` answers a model
  error with an apology, which would otherwise pass for the node's output; the
  run plugin records it and the run ends `failed` at that node.
- **Runs are owner-scoped**: another admin gets 404, as on another replica.
  Limits per person and per replica (429), and a rate limit on starts.
- **Cards** are fetched on first use or on `GET /studio/registry?resolve=true`,
  not at startup, and pinned on first use unless `card_sha256` is set
  (ADR-008, implementation notes).
- **MCP registry entries have no auth** in P0a: `headers_from_env` and token
  exchange are refused; secret-backed headers (`ORRERY_STUDIO_SECRET_*` only)
  are a follow-up.
- **The demo** has the plain `a2a-sdk` writer and Tirraflow's MCP server. The
  local time/units MCP server is in the CI test only, not the compose profile.
  The profile authenticates with HS256 dev tokens (`make dev-token
  CLIENT=orrery-console`), since the dev Keycloak client has no password grant.

### P1 as built (console)

- **Admin only, lazy.** The sidebar entry shows for the admin role; the view
  and `@xyflow/react` (12.12.0, pinned) load on first open, with their own
  CSS. The chat console's first load grows by 1.3 kB gzip of JS, and its CSS
  by 2.2 kB gzip (the Studio's Tailwind utilities live in the console's one
  utilities bundle: a second one would re-emit shared classes after the
  console's `dark:` variants and break them).
- **Capabilities.** What the server runs (kinds, patterns, routing, per-node
  models) is a client-side constant for P0a; the gallery and palette light up
  from it. The schema does not say so yet (see open items below).
- **Validation** is the client's own copy of the schema (a JSON Schema subset)
  and of the pipeline rules, against `/studio/schema` and the registry. The
  server's 422 is mapped by `loc`.
- **Not built:** ADK Python export, the MCP try-it call, replay from stored
  runs (P0b), editing on a phone. History is per tab, in memory.
- **Open items for the API:** a validate-only call; capabilities and budget
  ceilings in `/studio/schema`; MCP tool schemas and a read-only try-it call;
  a flag in `/me` that the Studio is on, so the entry is hidden instead of
  showing "not enabled"; stored runs.

### P0b as built

- **Graph compilation.** A spec compiles to one ADK 2.x `Workflow`: each agent
  node is a single-turn `LlmAgent`; routing, loop counters, edge screens,
  `parallel` and the join's combine step are `FunctionNode`s (`route_<id>`,
  `loop_<j>`, `screen_<j>`), and a `join` is ADK's `JoinNode` followed by a
  step that writes each branch's answer as a `## <branch>` block, in edge
  order. `plan_graph()` in `spec.py` checks the graph rules once and gives the
  compiler its plan, so a spec that validates compiles.
- **Labels are structured output**: a labelled node answers `{label, answer}`
  through ADK's `output_schema` (with tools, ADK's `set_model_response` tool,
  which the ledger does not count). An answer without a valid label fails the
  run with a field-level message, rather than taking an arbitrary branch.
  A label with no edge ends that branch; `POST /studio/validate` warns
  (`label_ends_run`).
- **Loop cap semantics**: `max_iterations: N` means the back edge is taken at
  most N times per entry of the loop. On the (N+1)th arrival `loop.iteration`
  says `stopped: true` and the branch ends with the answer the source was
  judging (its predecessor's: the last draft, not the critic's rejection), so
  the run still succeeds. A loop strictly inside another (its nodes a subset)
  starts from zero whenever the outer back edge is taken, so caps multiply:
  with two levels an inner body may run (5+1)² = 36 times. Loops therefore
  nest at most 2 deep (`loop_too_deep`); the run's budgets bound the rest.
- **No loop around a parallel section** (`loop_around_parallel`): ADK's
  `JoinNode` does not reset when its section is entered again, so a second
  round fired the join on the first round's answers (seen in review: nodes
  after the join ran 5 times for 3 rounds). Refused rather than compiled as a
  nested workflow; loop before the `parallel` or after the join.
- **Parallel** branches are chains of agents between one `parallel` and one
  `join` (2–6 branches); branches do not route, loop or fan out again (a
  nested `parallel` is refused). A
  `map_reduce` template uses a fixed fan-out: the map width is the branch
  count, not the data.
- **Supervisor** children are `llm_agent` or `supervisor` nodes compiled to
  `AgentTool`s (`n_<id>`), with plugins included so the run plugin traces them
  and counts each call as a delegation; at most 3 levels, one parent each.
- **Per-node models** come from `ORRERY_STUDIO_MODELS` and are resolved with
  `resolve_model(name)`: one model, never the deployment's `FallbackLlm` chain.
  `resolve_model()` with no name is unchanged. Every allowlisted model is
  built at startup (`studio/models.py`), and a bad name, missing `litellm` or
  missing provider key fails startup. Gemini safety filters are decided by
  the agent's model (`resolve_safety_config(gemini=...)`), not by
  `MODEL_PROVIDER`, so a Gemini node on another provider's deployment keeps
  them.
- **Trace entries** are keyed by agent name and invocation id: each
  `AgentTool` call of a child is its own invocation, so concurrent calls give
  two enter/exit pairs, and a node's tokens count per entry, not cumulatively.
- **Storage** is SQLAlchemy Core over four new tables, created with
  `create_all` (no migration of existing tables); the same SQL runs on SQLite
  in CI. Compositions are shared by admins and versioned; `PUT` and `DELETE`
  name the version they edit and get 409 with `current_version` otherwise.
  Runs are owner-scoped. Each run is written as it goes by a per-run writer
  (batched every 0.25 s, off the event loop), each batch with the run row in
  one transaction, so a reader that sees a run finished has all its events.
  The append skips sequence numbers already stored, so a batch whose commit
  was in doubt is retried whole; failures back off (to 5 s) and the final
  write is tried 8 times (its own attempts, after any failures mid-run). A
  finished run leaves the replica's memory only once its writer is done:
  pruning it earlier made the store show it `running` (a CI race). Every
  Studio statement on PostgreSQL has a 10 s `statement_timeout`, so a hung
  query cannot pin an executor thread (shared with chat) or the writer; a
  writer still there 5 minutes after its run ended is taken as stuck, and
  the run is forgotten anyway, with a warning. `create_all` runs under a PostgreSQL advisory lock,
  so replicas booting together do not race. At most 50 versions are kept per
  composition; saves are rate-limited (30/minute per person). Runs older than
  `ORRERY_STUDIO_RUN_RETENTION_DAYS` (14) are purged hourly. Without
  PostgreSQL, a bounded in-memory store keeps the same API.
- **Replicas.** Reads (`GET` run, history, `/events`) work on any replica: a
  stored run is replayed, or followed by polling (1 s, backing off to 5 s
  while idle; at most 4 followed runs per person) until it ends. Cancel
  stays on the running replica; elsewhere it is a 409. A run whose replica
  died reads as `failed` two minutes past its deadline.
- **`spec_version` stays 1**: P0b only gave meaning to fields P0a parsed and
  refused. `handoff`, `remote_a2a` and `human_approval` stay refused
  (`human_approval` runs since P0c).
- **For the console** (P1): `POST /studio/validate`, `capabilities` and
  `budget_ceilings` in `/studio/schema`, each MCP tool's description and input
  schema (as pinned) in `/studio/registry`, and `studio_available` in `/me`.
  An MCP "try it" call is not built.
- **Templates** are tested specs in `deploy/studio-demo/specs/`, one per
  pattern; the demo spec moved there as `critic-loop.json` and now loops.

### P0c as built

**Human approval** (`studio/approval.py`). The plan above paused the run with
a `RequestInput` interrupt and had the server inject the resume. A Studio run
is one asyncio task held in memory on the replica running it, so the node
simply **waits in that task**, on a future that only
`RunApprovals.decide()` resolves, reached only through
`POST /studio/runs/{id}/approvals/{aid}` for the run's owner (a human admin,
like every Studio route). There is no resume event, so there is nothing for a
client or a model to forge; the cost is the same as cancel's: a decision must
reach the running replica (elsewhere, 409).

- **Approve names a digest.** The person reads the text from
  `GET /studio/runs/{id}/approvals` (held in memory only, owner only) and
  approves its SHA-256; another digest is a 409. The text is **not redacted**:
  the first cut redacted credentials and emails while the digest, and the
  next node, had the original, so a person could approve
  `[REDACTED_EMAIL]` and send the real address on. Now what is shown is what
  runs on, the console recomputes the digest from what it shows, and text over
  128 KiB (served in part) can only be rejected. Reject needs no digest.
- **Once**, and **default deny**: a second decision is a 409; an approval
  undecided after `ORRERY_STUDIO_APPROVAL_TIMEOUT_SECONDS` (900, at most a
  day) expires, and an expiry always ends the run `rejected`, never takes an
  edge. The approval's future is the one arbiter: the expiry (`call_later`),
  a cancel (`cancel_all()`, called before the task is cancelled) and a
  decision each settle it, and a decision that loses gets a 409 rather than a
  200 the audit log would then contradict.
- **The deadline pauses.** The run's `timeout_s` is an `asyncio.timeout` the
  runner owns (the gateway's turn timeout is off for Studio runs), rescheduled
  while any approval waits; the ledger's deadline check subtracts the paused
  time. A person's time is not the run's compute budget.
- **Graph.** The node is an async `FunctionNode` that routes on
  `approved`/`rejected`. A rejection with a `rejected` edge sends that node the
  comment and the rejected text; without one the run ends `rejected`, and so
  does a rejection past the loop's `max_iterations` (a loop's "stop" would
  otherwise end the branch with the turned-down draft as the run's output).
  Approvals may not sit on a parallel branch or feed a `router`, and a run asks
  for at most 12 (a loop around one is also capped by `max_iterations`).
- **Trace.** `approval.requested` and `approval.decided` carry ids, the digest,
  size, expiry, decision and wait; content only with capture on. They are
  always kept when a run hits the event cap. Decisions are audited
  (`studio_approval_decided`).
- **Console.** An approval card in the Run tab (text, comment, time left;
  Approve stays off until the text is read), *awaiting you* on the node, label
  aware edge drawing, and a human-review template
  (`deploy/studio-demo/specs/human-review.json`).

**A remote's `input-required`** (`a2a/remote.py`, `StudioRunPlugin.request_input`).
`GuardedRemoteA2aTool` takes an optional `ask` callback. A Studio run passes
one that asks the run's owner through the same `RunApprovals`
(`kind="input"`), so the guarantees are the approval's: owner only, once,
against the digest of the question shown, default deny. The tool keeps one
ADK session open for the call, so the answer, sent with the reply's
`context_id`, continues the same task (ADK sets `task_id` from the session's
paused reply). The model never sees the question as something to answer:
it gets the final reply, or `input_required` (question blanked) with "do not
answer it yourself" when the person declines, the question expires or is
empty, or the remote asks a fourth time (`MAX_INPUT_ROUNDS = 3`). An asker
that cannot ask raises `InputUnavailable`, whose message the model gets:
no remote call left (charged, so the run ends `budget_exhausted` without
asking anyone), the run's budget spent while the person answered (the answer
is not sent), or `MAX_APPROVALS_PER_RUN` reached. Each answer is charged as a
remote call (`CompositionLedger.charge_remote`); the conversation's close is
bounded (`CLOSE_TIMEOUT_S`), and the per-message
`total_timeout_s` excludes the person's time. ADK hands a paused task's
status message on as thought, so the question is read from the wire
response. Outside the Studio (chat), there is no `ask` and nothing changes.

Still to come in P0c: `effect: acting` remotes and registry editing.

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

- [x] The demo pipeline runs end to end in CI (stub model, real `a2a-sdk` agent on a loopback socket, local MCP server). It emits `run.*`, `node.*`, `a2a.message` and `mcp.call` events.
- [x] `GuardedRemoteA2aTool` refuses (tests):
  - unregistered remotes;
  - a changed card digest;
  - RPC targets off the allowed origins;
  - redirects;
  - private and metadata addresses (unless allowlisted);
  - oversized cards and replies.
- [x] The remote agent receives only the tool request: a canary in an earlier tool result never reaches it (test).
- [x] A remote reply passes through the safety screen and PII redaction (test with an injection string and an email address).
- [x] Invalid specs are refused with a field-level error before any model call.
- [x] A studio run never changes `/admin/catalog`, the chat root or memory (test).
- [x] A service token with an admin role gets 403 on `/studio/runs`.

**P0b and later**

- [ ] Every node kind compiles and runs; a recorded run replays to the same graph state.
- [ ] A loop stops at its cap, and a ledger budget ends the run with `budget.hit`.
- [x] An approval can only be resumed by a human admin, once (P0c): the run's owner, against the digest of the text they read; expiry is a rejection (`test_studio_approval.py`).
- [ ] The console builds every pattern template, runs it and renders the live graph; axe-clean in both themes (P1).

## Open Questions

- Own deployment for the Studio (own catalogue, host and budgets) or a flag on the main one? Decided in P1.
- Cost estimates need AEP-015's price table. Until then, show tokens only?
- Should "publish as A2A" compositions be callable by the main coordinator as specialists?
