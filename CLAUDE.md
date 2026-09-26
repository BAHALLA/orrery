# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

Variants are **flags, not separate targets** (`SSO=1`, `PERSIST=1`, `MODE=socket|pubsub`,
`ROLE=`, `PROFILES=`). `make help` lists everything.

```bash
make install  # Python workspace (uv sync --all-extras) + web console (npm ci)
make check    # Full gate: lint + ty + Python tests + web gate — mirrors CI
make test     # Python tests only (~1200)
make test-web # Web console gate only (lint + format + types + tests + build)
make eval     # Run 33 agent eval scenarios (requires LLM credentials)
make lint     # ruff check + format check
make fmt      # Auto-fix lint + formatting, Python *and* web
```

Run tests for a single agent:
```bash
uv run pytest agents/kafka-health/tests/ -v
```

Infrastructure — `up`/`down` act on **everything**; `PROFILES` narrows it:
```bash
make up                     # All containers (Kafka, Postgres + pgAdmin, observability, Keycloak, Elasticsearch)
make up PROFILES=tracing    # Just the base stack + Tempo/Grafana
make down                   # Stop everything (volumes kept)
make reset                  # Stop + wipe volumes (fixes cluster.id mismatch); prompts unless FORCE=1
make ps                     # What's running
make logs SERVICE=kafka     # Tail logs
```

The `demo` and `slack` compose profiles are deliberately **excluded** from `make up`:
they run the agent itself in Docker on the same ports as `make run-api` / `make run-slack`,
so starting them by default would collide with local runs.

Docker demo (full stack with web UI on :8000):
```bash
docker compose --profile demo up -d --build   # (Makefile docker-* wrappers were removed)
docker compose --profile demo down
```

Run the orchestrator (orrery-assistant composes every specialist agent, so run it
directly rather than each agent standalone). **One target per surface** — there are
no per-agent run targets:
```bash
make run-dev            # ADK Dev UI (in-memory)
make run-cli            # Terminal mode
make run-cli PERSIST=1  # Persistent store (Postgres via DATABASE_URL, else in-memory)
make run-api            # FastAPI front door + web console on :8000 (auth ON, dev JWT)
make run-api SSO=1      # …with Keycloak SSO (needs `make up PROFILES=sso`)
make run-web            # Web console dev server with HMR (Vite :5173)
make run-slack          # Slack bot (MODE=socket for Socket Mode)
make run-chat           # Google Chat bot (MODE=pubsub for Pub/Sub mode)
make run-triage         # Deterministic triage Workflow, one batch run
make dev-token ROLE=…   # Mint a local JWT (viewer|operator|admin)
```

`run-api` builds the web console into `core/orrery_core/serving/static/` before
starting, so it is the single command for "the product". A missing `npm` warns and
serves the existing bundle rather than failing — Node stays optional for
Python-only contributors.

## Architecture

This is a **DevOps/SRE agent platform** built on **Google ADK** (Agent Development Kit). It uses a **uv workspace** with Python 3.14.

### Workspace Layout

- **`core/`** — Shared library (`orrery-core`): agent factory, multi-provider LLM support (Gemini/Claude/OpenAI/Ollama via LiteLLM), RBAC, config, guardrails, input validation, resilience (circuit breaker + retry), structured logging, audit trail, activity tracking, error handlers, persistent runner
- **`agents/`** — Independent agent packages, each runnable standalone or composable:
  - `kafka-health/` — Kafka cluster monitoring (14 Kafka-protocol tools inc. topic config get/tune/alter + consumer-group offset reset/delete, uses confluent-kafka) + 10 Strimzi operator tools
  - `k8s-health/` — Kubernetes cluster management (20 tools inc. services/endpoints, configmaps, and `top_pods`/`top_nodes` resource usage, uses kubernetes client) + 6 operator-aware tools
  - `elasticsearch/` — Elasticsearch cluster/index/shard diagnostics (19 REST tools) + ECK operator tools (5 tools)
  - `ops-journal/` — State management demo with 4 state scopes (session/user/app/temp)
  - `orrery-assistant/` — Multi-agent orchestrator that composes all above agents + Docker tools

### Key Design Patterns

- **Plugins over per-agent callbacks**: Cross-cutting concerns (RBAC, guardrails, autonomy, metrics, audit, activity tracking, resilience, output capping, error handling, prompt-injection screening, PII redaction) are packaged as ADK `BasePlugin` subclasses in `core/orrery_core/plugins/` and registered once on the `Runner` via `default_plugins()`. Plugins apply globally to every agent, tool, and LLM call. Order matters: `AuditPlugin` is registered **before** the gates (ADK's before-tool chain early-exits on the first non-None return, so audit records the attempt even when a gate denies the call), `AutonomyPlugin` before `GuardrailsPlugin` (the level is a property of the process, so a tool it forbids is refused rather than queued for an approval that cannot help it), and `ToolOutputCapPlugin` runs last among after-tool observers (it returns a replacement for oversized results, which would early-exit the after chain).
  **The one exception is per-tool confirmation**, and it is load-bearing: `GuardrailsPlugin` enforces RBAC only, while the human-in-the-loop gate for `@confirm`/`@destructive` tools is wired per agent as `before_tool_callback=require_confirmation()` (it must also work inside AgentTool sub-sessions and bare `adk web`, which no plugin registration covers). A new agent that omits that line keeps RBAC and silently loses confirmation — it has happened twice, to the docker specialist and then to `remediation_actor`. `agents/orrery-assistant/tests/test_confirmation_wiring.py` walks **both roots** and fails the build on an ungated agent; extend the walker, never the exception list.
- **Autonomy levels (L2/L3/L4)**: `AutonomyPlugin` gates by *process mode*, orthogonal to RBAC's *who*: L2 read-only (fail-closed — only unguarded tools + whitelist), L3 mutating with `@destructive` blocked (+ blacklist), L4 destructive allowed after ADK-native `request_confirmation`. Opt-in: registered only when `ORRERY_AUTONOMY_LEVEL` (or `default_plugins(autonomy_level=...)`) is set. A per-request override lives in `session.state["autonomy_level"]` but is honoured **only when written via `set_autonomy_level()`**, which also stamps `_autonomy_set_by_server` — session state is not a trust boundary (tools write to it, and tool output is attacker-reachable), so a bare value is ignored with a warning rather than promoting an L2 deployment to L4. Same defence RBAC has via `_role_set_by_server`. Refused calls return `{"status": "BLOCKED"}`; a call merely *paused* on an L4 confirmation returns `{"status": "AWAITING_CONFIRMATION"}` — the model reads that string, and "blocked" makes it report a one-answer-away action as failed. Both flow through the rest of the chain so audit/metrics observe them.
- **Blocking-tool concurrency**: every tool is `async def` around a blocking client and offloads via `asyncio.to_thread` / `run_in_executor(None, ...)` — both land on the loop's *default* executor. `configure_default_executor()` in `core/orrery_core/concurrency.py` replaces it with a pool sized from the **cgroup** CPU quota (v2 `cpu.max`, then v1, then the stdlib), because `os.cpu_count()` reports the host's cores, not the pod's limit. Called at startup by the HTTP front door (FastAPI lifespan) and `run_persistent`; override with `ORRERY_MAX_WORKER_THREADS`.
- **Cheap payload sizing**: `text_volume()` in `core/orrery_core/payload.py` sums string lengths over a structure instead of serializing it — O(nodes), not O(bytes). Used wherever a size *threshold* decides behaviour (PII offload, audit elision); serializing to make that decision would cost as much as the work being avoided.
- **Tool output cap**: `ToolOutputCapPlugin` bounds each tool result to `max_tool_result_bytes` (default 4 MiB; `0` disables) so one chatty `logs`/wide ES result re-sent every turn can't push the request past the Gemini/Vertex ~10 MiB limit (`400 INVALID_ARGUMENT`). Trimming is structure-preserving (longest string field / list kept element-wise, valid JSON) with a truncation note telling the model to narrow its query.
- **Async tools**: All tool functions are `async def` and use `asyncio.to_thread()`, `asyncio.create_subprocess_exec()`, or `_run_sync()` to offload blocking I/O (Kafka, K8s, Docker, HTTP) to thread pool executors.
- **Agent factory function** in `core/orrery_core/agent/base.py`: `create_agent()`. Multi-step orchestration uses ADK 2.0 graph `Workflow`s (see ADR-003), not the deprecated `SequentialAgent`/`ParallelAgent`/`LoopAgent` wrappers.
- **Output keys for data flow**: In multi-agent workflows (like `orrery-assistant`), sub-agents write results to session state via `output_key`; downstream agents read them.
- **RBAC via guardrail metadata**: `authorize()` in `core/orrery_core/security/rbac.py` infers minimum roles from `@destructive`/`@confirm` decorators (admin/operator/viewer). User role is read from `session.state["user_role"]`. Enforced globally via `GuardrailsPlugin`. A second, orthogonal axis answers *where*: `NamespaceScopeGuard` (opt-in via `ORRERY_PROTECTED_NAMESPACES`, comma-separated globs) refuses non-admin **mutations** targeting a protected namespace, resolving the effective namespace from the call's argument or the tool's signature default and failing closed when it can't (required-but-missing, or a non-string). Reads are never scoped. See `docs/adr/001-rbac.md`.
- **Input validation**: `core/orrery_core/security/validation.py` provides `validate_string()`, `validate_positive_int()`, `validate_url()`, `validate_path()`, `validate_list()` — all tools validate inputs at entry using the walrus operator pattern: `if err := validate_string(...): return err`.
- **Guardrails as decorators**: `@destructive(reason)` and `@confirm(reason)` attach metadata to tool functions. `GuardrailsPlugin` reads this metadata at runtime. Confirmations use args-hash + TTL to prevent bypass. Two modes: model-mediated (the default for bare `AgentGateway`/`adk web`/evals — a re-call in a new invocation counts as confirmed) and **requester-verified** (`AgentGateway(verified_confirmation=True)` — enabled on every shipped exposition: HTTP server, persistent runner, Slack bot, Google Chat bot) where the gate additionally requires a human decision recorded by the gateway — a deliberate word (`approve`/`confirm`/`proceed`/`go ahead`; a casual "ok"/"yes" doesn't count, deny is broad) sent by the *same verified actor* who triggered the pending action. Fail-closed: unknown requester, a second person's approval, or a decision that **predates the pending action** is refused — the gateway rewrites the decision key every turn (clearing it when the message isn't a decision) and the gate requires `decision.timestamp >= pending.created_at`, so an "approve" said before an action existed can never authorize it. The Slack/Google Chat bots gate through their own confirmation cards instead (`slack_confirmation` / `google_chat_confirmation`) with the same requester-only rule (`approval_refusal` / `_refuse_non_requester`); Deny stays open to anyone. Google Chat's decision channel is transport-dependent: reply `approve`/`deny` in the card's thread (default — the only channel that works over Pub/Sub, where a button click's synchronous round-trip can't complete), or inline buttons on HTTP deployments via `GOOGLE_CHAT_INTERACTIVE_BUTTONS=true`.
- **Per-turn caller identity**: `create_agent()` wraps every instruction in an `identity_aware_instruction` provider that appends "who you are talking to" when a transport stamped the turn's `actor` into state (`AgentGateway` stamps `msg.user_id` automatically; `_auth.subject` is the fallback) — so in shared threads the model acts for the current sender and never reports a tool's service account as the user. Side effect: instructions are used **verbatim** (no `{var}` state templating — literal braces are safe). Tests read prompt text via `base_instruction(agent)`.
- **Authentication enforcement**: `set_user_role()` marks roles as server-trusted. `GuardrailsPlugin` calls `ensure_default_role()` via `before_agent_callback` to force `viewer` if the role wasn't set by the server, preventing privilege escalation.
- **Runtime content defenses (AEP-013, all default-on)**: `SafetyScreenPlugin` screens **both directions**, differently, because the trust story differs. *Direct*: an injected user message is **blocked** in `before_run_callback` (the only plugin hook whose non-None return halts the runner), so it costs no tokens and reaches no tool. *Indirect*: tool results are screened in `after_tool_callback` and matched spans **neutralized in place** (`FILTER_MARKER`) rather than dropped — a pod annotation, log line, k8s event or ES document is attacker-reachable text arriving with a tool result's authority, but it is also the evidence the agent was asked to read, so rejecting the payload would break the diagnosis. Same in-place/return-`None` contract as PII redaction, and the same `OFFLOAD_THRESHOLD_CHARS` thread hop for multi-MiB payloads (`ORRERY_SAFETY_SCREEN=false` disables both). `PIIRedactionPlugin` scrubs credentials from tool results by **mutating in place and returning None** — a returned copy would early-exit ADK's after-tool chain and skip later observers — and registers before `AuditPlugin` so audit records redacted values. It sees the *uncapped* result (`ToolOutputCapPlugin` must stay last), so on multi-MiB payloads it is the chain's dominant cost: each value pattern declares the case-sensitive literals it cannot match without (`AKIA`, `ghp_`, `eyJ`, …) and is skipped when they are absent (~2x on clean log text), and redaction moves to a worker thread above `OFFLOAD_THRESHOLD_CHARS` so it stops holding the event loop (`ORRERY_PII_REDACTION=false`; `ORRERY_REDACT_IPS=true` adds IPv4 redaction, off by default). `create_agent()` attaches Gemini safety settings at `BLOCK_ONLY_HIGH` to Gemini string models only (`GEMINI_SAFETY_FILTERS`/`GEMINI_SAFETY_THRESHOLD`).
  **Result shapes both plugins must handle:** ADK does *not* require a tool to return a dict — `FunctionTool.run_async` returns the function's value verbatim and the `{"result": …}` normalization happens in `__build_response_event`, *after* the after-tool chain. A dict/list-only walk therefore skipped two live shapes: a bare string (nothing in-repo returns one, but nothing stops it) and a Pydantic model — which `load_memory`, wired into the shipped chat root, does return. Both walks now also traverse **object attributes** (`payload.mutable_attributes`), so a `LoadMemoryResponse` is scrubbed in place with the chain intact; a bare `str`/`bytes` cannot be mutated at all, so it is scrubbed by *returning* the replacement, which early-exits the chain and is logged as such (losing one audit outcome line beats writing a credential into it). Write-side parity matters too: `SecureMemoryService` shares `security/redaction.py` with the plugin, because its own shorter list once stored bare `ghp_`/`AKIA`/JWT tokens verbatim that the tool path would have caught — and `load_memory` handed them back.
- **Memory event de-duplication is the database's job**: `orrery_memory_events` carries a unique index on `(app_name, user_id, session_id, event_id)` and `_add_events_sync` inserts with `ON CONFLICT DO NOTHING`. It used to read the session's existing ids and filter the batch against them in Python — correct single-threaded, but two overlapping turns in one session (a shared Slack thread, two webhooks) both read the same snapshot, both find the ids absent, and both insert; a barrier-synchronized 8-writer repro duplicated on 2 runs in 3. Recall then returns the event twice and pays for it twice in the model's context. `_add_session_sync` needs the same `DO NOTHING` because its delete-then-insert cannot see a concurrent writer's uncommitted rows. Two non-obvious parts: (1) there is no Alembic here and `create_all` only creates missing *tables*, so `_ensure_event_uniqueness()` back-fills the index onto existing deployments — deduping first, because the index cannot be built over rows the old path already duplicated — and runs both steps only when the index is absent, keeping it a one-time cost rather than a per-boot self-join; (2) `event_id` is nullable and Postgres treats NULLs as distinct in a unique index, so the constraint rests on ADK stamping every `Event` with a UUID (it replaces a missing *or empty* id in a `model_validator`) — `test_database_memory.py` pins that upstream guarantee rather than trusting it.
- **Structured JSON logging**: `setup_logging()` configures JSON output to stdout (called automatically by `load_agent_env()`). `AuditPlugin` emits tool-call audit entries via the logging system; because it runs *before* the output cap, responses over `MAX_AUDIT_RESPONSE_CHARS` are recorded as status + measured size rather than serialized whole (a 20 MiB result was a 20 MiB log line, per call). `ActivityPlugin` records tool calls to session state for cross-agent visibility, capped at `MAX_SESSION_LOG_ENTRIES` — ADK copies the whole assigned list into each event's state delta, so an unbounded log makes a session's write volume grow quadratically with its length.
- **Connection pooling**: Kafka `AdminClient`, K8s API clients, and HTTP sessions are cached as module-level singletons to avoid per-call connection overhead. **Every Kubernetes API class must be built on `shared_api_client()`** (`core/orrery_core/tools/kube.py`, `orrery-core[kubernetes]`), never bare `client.CoreV1Api()`: the `kubernetes` client otherwise sends *no* timeout, and a hung API server then holds a worker thread from the deliberately small default executor forever. The shared client fills in a `(connect, read)` timeout and a retry policy that never multiplies it.
- **Multi-provider LLM**: `resolve_model()` in `core/orrery_core/agent/base.py` reads `MODEL_PROVIDER` + `MODEL_NAME` env vars. For Gemini returns a string; for others returns `LiteLlm(model=...)`. All agents use this via `create_agent()` — no per-agent changes needed.
- **Reply-text extraction**: All user-facing transports (Google Chat, Slack, HTTP `/chat`, CLI) build the response by funneling runner events through `extract_reply_text()` in `core/orrery_core/serving/events.py`. It concatenates part text but skips ADK "thought" parts (`part.thought is True`) — Gemini native thinking, `PlanReActPlanner` planning phases, and LiteLLM-surfaced provider reasoning are all normalized onto that flag — so planner/thinking output never leaks into a user reply regardless of provider. Add a new transport? Call this helper rather than iterating `content.parts` yourself.
- **Prometheus metrics**: `MetricsPlugin` in `core/orrery_core/plugins/` wraps `MetricsCollector` to track tool call counts, latency histograms, error rates, circuit breaker state, and LLM tokens globally. `start_server(port=9100)` exposes `/metrics` for Prometheus scraping.
- **Distributed tracing (OpenTelemetry)**: `core/orrery_core/observability/tracing.py` provides `configure_tracing()` (installs a global `TracerProvider` → OTLP exporter, idempotent, gated by `OTEL_TRACING_ENABLED`) and `TracingPlugin`. ADK 2.0 already emits native spans for agent/tool/LLM calls under the `gcp.vertex.agent` tracer, so `TracingPlugin` **enriches the current span** (`orrery.request_id`, `orrery.user_role`, `orrery.tool.status`/`result_size`, exception recording) rather than creating duplicate spans; `after_model` only bridges tokens to `track_llm_tokens()` since ADK already sets `gen_ai.usage.*`. `default_plugins(enable_tracing=None)` resolves from `OTEL_TRACING_ENABLED` and prepends the plugin first, so a single env flag turns tracing on across every transport — a missing `[otel]` extra is a skip-with-warning, not a crash. Requires `orrery-core[otel]`; imported lazily (not re-exported from `__init__.py`), mirroring `server.py`/`[server]`. Log↔trace correlation: `JSONFormatter` (`log.py`) stamps `request_id` (a ContextVar, dependency-free) plus `trace_id`/`span_id` (lazy OTel) onto every record. Local stack: `make up PROFILES=tracing` (Tempo + Grafana under the `tracing` compose profile, with a provisioned `Orrery — Agent Observability` dashboard).
- **Resilience**: `ResiliencePlugin` in `core/orrery_core/plugins/` wraps `CircuitBreaker` for per-tool circuit breaking globally. `@with_retry` decorator adds exponential backoff with jitter to async tool functions.
  **The breaker counts outcomes in `after_tool_callback`, not `on_tool_error_callback`** — every tool here catches its own exception and returns `{"status": "error", ...}` rather than raising, so the error hook almost never fires and a Kafka outage looks, from its point of view, like nothing happening. Recording success for any call that merely *completed* reset the counter on precisely the calls the breaker exists to count, and it could not open for the failure mode it was built for. `classify_tool_outcome()` (`reliability/resilience.py`) now maps a result to SUCCESS / FAILURE / **IGNORE**, and the third value is the load-bearing one: a gate's answer (`BLOCKED`, `AWAITING_CONFIRMATION`, `access_denied`, `confirmation_required`) or the breaker's own `CircuitOpen`/`CircuitHalfOpen` refusal means the tool never ran, so it is evidence in neither direction — counting those as failures would let every blocked call re-stamp `_opened_at` and wedge the circuit permanently open. An IGNORE also releases a half-open probe slot, because `require_confirmation()` is an *agent* callback and can therefore answer after this plugin has already marked a probe in flight. Deliberate false positive: a tool answering "topic not found" with `status: "error"` counts as a failure, which is safe only because the counter is *consecutive* and resets on the first success. The neutral statuses are duplicated as literals to keep `reliability` free of a `plugins` import cycle; `core/tests/test_resilience.py` pins them to their real definitions so a rename fails the build.
- **Session maps are capacity-bounded**: `BoundedSessionCache` (`core/orrery_core/serving/session_cache.py`) backs both `MappedSessionResolver` and the Slack bot's `SessionMap`. Each maps a conversation key to an ADK session id, each gains an entry per participant per thread, and neither has any production path that removes one — `forget`/`remove` serve an explicit "start over", not eviction — while both live in processes that run for weeks (the Slack and Chat bots hold one as a module-level singleton). They are lookup shortcuts over the durable session store, so a miss just creates a fresh session: LRU eviction costs the least-recently-used thread exactly what a restart already costs it, which makes bounding them the cheap correct fix rather than a tradeoff. Both consumers share the one primitive because the same bug was written twice independently.
- **Context caching**: `create_context_cache_config()` in `core/orrery_core/serving/runner.py` creates an ADK `ContextCacheConfig` with env-var defaults (`CONTEXT_CACHE_MIN_LENGTH`, `CONTEXT_CACHE_TTL_SECONDS`, `CONTEXT_CACHE_INTERVALS`). Only effective with Gemini models. Enabled in orrery-assistant via the `App` object.
- **Context compaction (AEP-020, default-on)**: caching shrinks what a request *costs*; compaction shrinks what it *contains*. `create_events_compaction_config()` (same module) configures ADK's **native** `EventsCompactionConfig` on the `App` — past `ORRERY_COMPACTION_TOKEN_THRESHOLD` (250k) ADK replaces older events with an LLM digest, keeping the last `ORRERY_COMPACTION_RETENTION_EVENTS` verbatim. Without it a long incident session grows until the request exceeds the model window: `ToolOutputCapPlugin` bounds one 4 MiB tool result, never the accumulated transcript, and three of those already approach Gemini's ~10 MiB ceiling. Lossy for the model, **lossless for the record** — ADK appends the digest as an event carrying the compacted timestamp range and filters the originals only at request-assembly time, so audit/replay are untouched. Three things are non-obvious: (1) the summarizer is **always** passed explicitly — ADK otherwise derives it from the root agent's model, which bills digests at the agent's rate and raises outright for a non-`LlmAgent` root (`orrery_triage_workflow`); (2) `compaction_interval`/`overlap_size` are required by ADK, so the sliding-window backstop **cannot be disabled** — its default is set high so the token trigger normally fires first; (3) compaction events bypass `on_event_callback` (the Runner appends them after the agent generator is exhausted), so the metric hook lives in `_ObservedEventSummarizer`, not `MetricsPlugin`. Threaded through every `App`/`AgentGateway` site; `ORRERY_CONTEXT_COMPACTION=false` disables.
- **Closed-loop remediation**: the remediation subgraph in `agents/orrery-assistant/orrery_assistant/remediation.py` runs act → verify → retry as `remediation_actor → remediation_verifier → verify_route` wired by a `RoutingMap` (`{"retry": actor, "done": summarizer}`). The verifier calls `mark_remediation_resolved` to signal success; `verify_route` enforces the 3-iteration cap via a state counter (replaces the deprecated `LoopAgent` + `exit_loop`/`escalate`). The actor wires `require_confirmation()` like every other tool-calling agent — non-negotiable, because this is the one root that runs with no human in the conversation: `run_triage.py` pins `operator`, which RBAC lets past `@confirm` tools such as `scale_deployment`, so before the gate existed an unattended cron sweep could rescale a deployment the model chose. See [ADR-003](docs/adr/003-graph-workflow-inversion.md).
- **Pydantic-settings config**: Each agent subclasses `AgentConfig` for typed env var loading from `.env` files colocated with the agent module.
- **All tests use mocks**: `@patch` on internal client getters (e.g., `_get_admin_client`). All tool tests are `async` with `@pytest.mark.asyncio`. No running Kafka/K8s/Docker required. Autouse fixtures reset cached clients between tests.
- **Agent evals** (`make eval`): 33 scenarios across 5 specialist agents (kafka, k8s, elasticsearch, observability, docker) using ADK's `AgentEvaluator`. The orrery-assistant root no longer has a routing eval — its graph root is exercised by deterministic unit tests instead (see ADR-003). Each agent has `tests/evals/` with `.test.json` datasets and a `test_*_eval.py` runner. Evals use a real LLM (gated behind `@pytest.mark.eval`) with mocked external dependencies. Criteria: `tool_trajectory_avg_score >= 1.0` (exact tool call match). The whole workspace collects in one `pytest` run via `--import-mode=importlib` (configured in the root `pyproject.toml`), so duplicate test basenames across agents (e.g. `test_app.py`, `test_handler.py`) no longer collide.

### orrery-assistant: chat root + deterministic triage Workflow (ADK 2.0)

There are **two roots** that reuse the same node agents (see
[ADR-003](docs/adr/003-graph-workflow-inversion.md), supersedes ADR-002):

1. **Interactive root** — `orrery_chat_agent`, a chat-mode `LlmAgent`
   (`mode="chat"`, set via `create_agent(mode=...)`). It holds real conversation
   history and routes free-form queries to the six specialist `AgentTool`s
   (kafka/k8s/observability/elasticsearch/docker/ops-journal), plus an
   `incident_triage_agent` `AgentTool` for single-turn full sweeps and a
   `LoadMemoryTool` (model-invoked `load_memory` for past-incident recall — the
   coordinator decides when to search memory rather than preloading every turn).
   This is the root for `adk web` / CLI / Slack / Chat,
   hosted by `App(root_agent=orrery_chat_agent)`. A chat-mode agent **must** be a
   root — ADK 2.0 forbids it as a routed node inside a graph.
2. **Batch root** — `orrery_triage_workflow`, a graph `Workflow` run by
   `make run-triage` for scheduled/batch incident response. A `Workflow` is not a
   `BaseAgent`, so it can't be an `AgentTool`/sub-agent of the chat root — hence the
   two are separate entrypoints. `create_agent()` LlmAgents are graph **nodes** (an
   `LlmAgent` is a `BaseNode` in ADK 2.0); routing is pure-Python `FunctionNode`s.
   Edges are chain-tuples (sequential), node-tuples (parallel), and `RoutingMap`
   dicts (conditional/loop); a `JoinNode` is the parallel fan-in barrier. Routing
   functions set `ctx.route`; nodes share data via `ctx.state` / `output_key`.
   `runner.py`/`server.py` accept `Agent | Workflow`.

```
orrery_chat_agent (chat-mode LlmAgent, interactive root)
  ├─ AgentTool: kafka / k8s / observability / elasticsearch / docker / ops_journal
  ├─ AgentTool: incident_triage_agent (single-turn full health sweep)
  └─ LoadMemoryTool (model-invoked past-incident recall)

orrery_triage_workflow (Workflow, batch root — `make run-triage`)
  START ─▶ [parallel] kafka/k8s/docker/observability/elasticsearch checkers
        ─▶ health_join (JoinNode, waits for all 5)
        ─▶ triage_summarizer (record_triage_verdict → incident_severity)
        ─▶ journal_writer ─▶ triage_route
              ├─("remediate")─▶ remediation_actor ⇄ remediation_verifier
              │                     └▶ verify_route ─("retry")▶ actor
              │                                     └("done")▶ summarizer ─▶ final_report
              └─("resolved")────────────────────────────────────▶ final_report
```

`triage_route` fails safe: if the LLM skips `record_triage_verdict`, it infers severity
from the per-system status reports and flags `triage_verdict_missing` rather than
silently resolving. The deprecated `create_sequential_agent` / `create_parallel_agent` /
`create_loop_agent` factories were removed — compose multi-step flows as graph
`Workflow`s instead.

## Code Style

- **Ruff** for linting and formatting (line-length: 100, target: py314)
- Lint rules: E, W, F, I (isort), UP, B, SIM
- Known first-party packages configured in `[tool.ruff.lint.isort]`
- CI runs both `ruff check` and `ruff format --check`
