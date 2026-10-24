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
| Patterns | Sequential, parallel + `JoinNode`, router, critic loop and agent-as-tool exist in ADK 2.12 and in the triage `Workflow` | Hand-written Python only; the `Workflow` root runs only from `make run-triage` |
| Runtime editing | `PUT /admin/catalog`: declarative agents (instruction + catalogue tools) and on/off switches | No topology, no toolsets or nested agents, and a save replaces the **live** tree for everyone |
| Models | One process-wide `MODEL_PROVIDER`/`MODEL_NAME` (+ fallback chain) | No per-agent model, so no "same pattern, two models" comparison |
| MCP | Hardened client (`GuardedMcpToolset`), three transports | Servers fixed in `ORRERY_MCP_CONFIG` at startup |
| A2A | Inbound only ([ADR-007](../adr/007-a2a-exposition.md)) | No way to call a remote agent |
| Trace | `ProgressStep` tree over SSE (`/chat/stream`) | No node enter/exit, route decisions, per-node tokens, or A2A message timeline |
| Console | Chat, `RunTrace` chips, Agents form | No canvas, no draft, no run history |

ADK's own declarative format (`AgentConfig`, `agents/config_schemas/AgentConfig.json`)
is not the answer: it resolves models, tools and callbacks through
`CodeConfig` **Python import paths**. An API that accepts it accepts arbitrary
imports. It also has no graph `Workflow`.

The goal is a sandbox where an operator builds a composition (local agents,
remote A2A agents, MCP servers, wired in a named pattern), runs it against a
draft that touches nothing live, and watches it execute. That serves three jobs
the product cannot do today: demonstrating A2A and MCP, testing third-party
agents and servers safely, and comparing patterns.

## Proposed Solution

### Non-goals (v1)

- Public or anonymous use. v1 is **admin only** (`orrery-admin`).
- stdio MCP servers in compositions (that is running someone else's code on
  the host). HTTP transports only.
- Changing the live coordinator. Studio runs are ephemeral and never call
  `replace_root_agent()`.
- Arbitrary egress. Every remote endpoint comes from an admin-curated registry.

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
    { "id": "writer", "kind": "remote_a2a", "endpoint": "demo-langgraph-writer",
      "skills": ["draft"] },
    { "id": "critic", "kind": "llm_agent", "model": "openai/gpt-5-mini",
      "instruction": "Reject unless every claim cites a source." },
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
| `llm_agent` | `create_agent()` (so every guard, plugin and confirmation hook applies) with the node's model via `resolve_model()` |
| `remote_a2a` | `GuardedRemoteA2aAgent` ([ADR-008](../adr/008-consuming-remote-a2a-agents.md)) from a registry entry |
| `mcp_server` | `GuardedMcpToolset` built per run from a registry entry; its tools are referenced as `mcp:<entry>.<tool>` |
| `router` | `FunctionNode` setting `ctx.route` from the previous node's structured output (`when` labels on edges) |
| `parallel` / `join` | Node tuple + `JoinNode` |
| `loop` | An edge back with `max_iterations` (capped, like `verify_route`) |
| `supervisor` | `llm_agent` whose children are wrapped as `AgentTool`s |
| `human_approval` | The existing requester-verified confirmation gate |
| `guardrail` | `SafetyScreenPlugin` / PII redaction applied to the edge's payload |

`pattern` is a hint for the console's templates and for the compiler's checks
(a `router` pattern must have labelled edges); the graph is what runs.

**Validation, before anything runs:** at most 30 nodes, 6 parallel branches,
3 levels of supervisor nesting, `max_iterations` ≤ 5, a reachable terminal
node, no unlabelled cycles, models from an allowlist (`ORRERY_STUDIO_MODELS`),
and tools only from the catalogue or the registry. Budgets cannot exceed the
deployment's ceilings.

### 2. Ephemeral runs

`orrery_core/studio/` adds a runner beside `AgentGateway`:

- `POST /studio/runs` with `{composition_id | spec, input}`, admin only:
  compile, validate, run. Each run gets a session with the `s-` prefix, its own
  budgets (composed with AEP-027) and the deployment's autonomy ceiling
  (**L2** by default; a human approval node still works because confirmation
  is independent of autonomy).
- `GET /studio/runs/{id}/events` (SSE) and `GET /studio/runs/{id}` (the stored
  run).
- `POST /studio/runs/{id}/cancel`.
- Compositions are versioned documents in Postgres
  (`studio_compositions`, optimistic concurrency like `catalog_store`). Runs and
  their events are kept for `ORRERY_STUDIO_RUN_RETENTION_DAYS` (default 14).

The batch `Workflow` limitation (CLI only) is removed for studio runs only: the
runner drives `Workflow` roots through the same plugin stack the gateway uses.

### 3. Trace events v2

Superset of `ProgressStep`, on the studio SSE stream only:

| Event | Fields |
|-------|--------|
| `run.start` / `run.end` | composition version, totals (tokens in/out per model, tool calls, wall time, estimated cost from AEP-015 prices) |
| `node.enter` / `node.exit` | `node_id`, `kind`, `model`, tokens, `elapsed_ms`, outcome |
| `route.decision` | `node_id`, chosen label, alternatives |
| `loop.iteration` | edge, iteration, cap |
| `a2a.message` | direction, JSON-RPC method, task id, task state transitions, part kinds and sizes; text only when `ORRERY_STUDIO_CAPTURE_CONTENT=true` |
| `mcp.call` | entry, tool, classification, outcome, `elapsed_ms` |
| `approval.requested` / `approval.decided` | as AEP-024 |
| `budget.hit` | which budget |

Arguments and results stay off the wire by default, as in `ProgressPlugin`. A
run's events are what the console renders, replays and compares.

### 4. The endpoint registry

`studio_endpoints`, edited by admins in the console:

```yaml
- id: tirraflow
  kind: mcp
  url: https://tirraflow.com/mcp
  transport: streamable_http
  tools: { search_articles: read_only, get_article: read_only, list_series: read_only }
- id: demo-langgraph-writer
  kind: a2a
  card_url: http://studio-demo-writer:8080/.well-known/agent-card.json
  skills: [draft]
  effect: read_only            # acting -> every delegation needs confirmation
  private_network: true        # only for the demo fleet's own network
```

Every entry goes through the existing MCP network guard (single origin, no
redirects, public addresses unless `private_network`, metadata/link-local
always blocked). Secrets are references to env vars, never stored values.

### 5. The Studio console

A `studio` view in `web/` (React 19, TypeScript, Tailwind 4), with
[`@xyflow/react`](https://reactflow.dev) for the canvas:

- **Canvas:** palette of node kinds, pattern templates (pipeline, fan-out/fan-in,
  router, supervisor, critic loop, debate, map-reduce, handoff, plan-and-execute),
  per-node inspector, live validation against the published JSON Schema.
- **Run panel:** the graph animates from trace events (active node, route taken,
  loop counter, tokens and latency per node); a timeline view of the same events.
- **A2A inspector:** fetch a registry card, show skills and capabilities, and a
  message/task-state timeline for each `remote_a2a` node.
- **MCP inspector:** list a registry server's tools and schemas, classification,
  and a try-it call (read-only tools only).
- **History and compare:** past runs of a composition, replay, and two runs side
  by side (pattern A vs B, model A vs B) with totals.
- **Export:** the spec as JSON/YAML, and generated ADK Python for the
  composition (read-only output, never executed by the server).

### 6. The demo fleet

A compose profile `studio-demo` with small, signed images on an internal
network, so A2A across frameworks can be shown without opening egress:

- an ADK agent exposed with Orrery's A2A adapter;
- a LangGraph agent and a plain `a2a-sdk` agent (different frameworks, same
  protocol);
- MCP: Tirraflow's own public `/mcp`, plus one small local server (time/units).

### Phases

1. **P0, runtime:** spec + compiler + validation, ephemeral runner, trace events
   v2, per-node models, the registry, `GuardedRemoteA2aAgent` (ADR-008).
   No UI beyond the API.
2. **P1, Studio v1 (admin only):** canvas, templates, run panel, A2A and MCP
   inspectors, demo fleet. Deployed as its own host
   (`studio-orrery.tirraflow.com`) and listed in the Tirraflow labs catalog.
3. **P2, frontier features:**
   - **Publish as A2A:** a saved composition gets its own agent card and is
     served by the ADR-007 adapter, so compositions can call compositions.
   - **Eval sets:** run a composition over a dataset with an LLM judge
     (AEP-002 harness), and compare patterns on scores, cost and latency.
   - Share a run as a read-only replay link.
4. **P3, others' hands:** invited or public users, per-learner isolation on a
   separate runner host (Tirraflow TEP-007), bring-your-own endpoints.

### Affected Files

- New: `core/orrery_core/studio/{spec,compiler,runner,events,registry,store}.py`,
  `core/orrery_core/a2a/remote.py` (ADR-008), migrations for
  `studio_compositions`, `studio_runs`, `studio_run_events`, `studio_endpoints`.
- `core/orrery_core/serving/server.py`: mount `/studio/*` when
  `ORRERY_STUDIO_ENABLED=true`.
- `core/orrery_core/agent/base.py`: `resolve_model(model=None)` accepts a
  per-node model from the allowlist.
- `web/`: `src/studio/*`, `@xyflow/react` dependency.
- `deploy/helm/orrery-assistant/`: studio values; `docker-compose.yml`:
  `studio-demo` profile.
- Docs: `docs/integrations/studio.md`, this AEP, ADR-008.

## Security

| Risk | Control |
|------|---------|
| Arbitrary imports through a spec | The spec references catalogue tools and registry entries by id only; no import paths, no code fields |
| SSRF through MCP or A2A URLs | Registry only, admin-edited, existing network guard; `private_network` only for the demo network |
| A remote agent acting on our behalf | `effect: acting` entries require confirmation per delegation; replies screened by `SafetyScreenPlugin` before the next node sees them |
| Prompt injection travelling between nodes | Guardrail edges; every remote reply and MCP result is untrusted input, as today |
| Cost | Per-run budgets, model allowlist, `ORRERY_DAILY_TOKEN_BUDGET_PER_USER` |
| Leaking content into traces | Arguments, results and message text off by default |
| Changing production behaviour | Runs are ephemeral; the live tree is never replaced |

## Acceptance Criteria

- [ ] A spec with every node kind compiles, validates and runs end to end in a test.
- [ ] Invalid specs (cycles without caps, unknown tools, over-limit) are refused with a field-level error, before any model call.
- [ ] A studio run never changes `/admin/catalog` or the chat root (test).
- [ ] Trace events v2 are emitted for every node kind; a recorded run replays to the same graph state.
- [ ] `GuardedRemoteA2aAgent` refuses unregistered cards, redirects, private addresses (unless allowed) and oversized replies (tests).
- [ ] The console builds every pattern template, runs it and renders the live graph; axe-clean in both themes.
- [ ] Demo fleet: one composition crossing ADK, LangGraph and plain a2a-sdk agents plus two MCP servers runs green in CI.

## Open Questions

- A Studio deployment of its own (recommended, own host and budgets) or a flag on the main one?
- Cost estimates need AEP-015's price table; until then show tokens only?
- Should "publish as A2A" compositions be callable by the main coordinator as specialists?
