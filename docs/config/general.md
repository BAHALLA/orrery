# General Configuration

The platform's core behavior, including LLM providers and infrastructure services, is controlled via environment variables.

## LLM Provider

The platform supports multiple LLM providers through [LiteLLM](https://docs.litellm.ai/). Switch providers by setting two environment variables — no code changes needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PROVIDER` | `gemini` | LLM backend: `gemini`, `anthropic`, `openai`, `ollama`, etc. |
| `MODEL_NAME` | `gemini-3.6-flash` | Model identifier (provider prefix auto-added if missing) |
| `GEMINI_MODEL_VERSION` | unset | Gemini only: used when `MODEL_NAME` is unset. `MODEL_NAME` wins when both are set. |

### Provider examples

<details>
<summary>Google Gemini (Default)</summary>

```env
MODEL_PROVIDER=gemini
MODEL_NAME=gemini-3.6-flash
# Either Vertex AI:
GOOGLE_GENAI_USE_ENTERPRISE=TRUE
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
# Or AI Studio:
GOOGLE_GENAI_USE_ENTERPRISE=FALSE
GOOGLE_API_KEY=your-api-key
```

`GOOGLE_GENAI_USE_ENTERPRISE` replaces `GOOGLE_GENAI_USE_VERTEXAI`, which ADK
2.12 deprecated. The old name still works (ADK logs a `DeprecationWarning`); if
both are set, `GOOGLE_GENAI_USE_ENTERPRISE` wins, as it does in google-genai.
</details>

<details>
<summary>Anthropic Claude</summary>

```env
MODEL_PROVIDER=anthropic
MODEL_NAME=anthropic/claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-api03-...
```
</details>

<details>
<summary>OpenAI</summary>

```env
MODEL_PROVIDER=openai
MODEL_NAME=openai/gpt-4o
OPENAI_API_KEY=sk-...
```
</details>

<details>
<summary>Ollama (Local)</summary>

```env
MODEL_PROVIDER=ollama
MODEL_NAME=ollama/llama3
OLLAMA_API_BASE=http://localhost:11434
```
</details>

### Getting API keys

| Provider | How to get a key | Env var |
|----------|-----------------|---------|
| **Google AI Studio** | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `GOOGLE_API_KEY` |
| **Google Vertex AI** | GCP Project + `gcloud auth application-default login` | `GOOGLE_CLOUD_PROJECT` |
| **Anthropic** | [console.anthropic.com](https://console.anthropic.com/settings/keys) | `ANTHROPIC_API_KEY` |
| **OpenAI** | [platform.openai.com](https://platform.openai.com/api-keys) | `OPENAI_API_KEY` |
| **Ollama** | Install [Ollama](https://ollama.com/), run `ollama pull llama3` | N/A |

### Model-call retry and fallback

Tool calls have had a circuit breaker and retries for a long time; the model call
itself now has two layers of its own ([AEP-021](../enhancements/aep-021-provider-fallback.md)):

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_LLM_RETRY_ATTEMPTS` | `4` | **Total** tries per model call on `408`/`429`/`5xx`, with exponential backoff and jitter. `0` or `1` disables retrying. Applied to every agent: Gemini reads it natively, LiteLLM receives it as `num_retries`. |
| `ORRERY_LLM_RETRY_INITIAL_DELAY` | `1` | First backoff, seconds. |
| `ORRERY_LLM_RETRY_MAX_DELAY` | `30` | Backoff ceiling, seconds. |
| `MODEL_FALLBACK_CHAIN` | unset | Comma-separated models tried in order after the primary. Same family as the primary: bare Gemini names for `MODEL_PROVIDER=gemini`, `provider/model` (or a bare name, prefixed with `MODEL_PROVIDER`) for LiteLLM. |
| `MODEL_FALLBACK_COOLDOWN_SECONDS` | `30` | After a model fails, how long it is tried *last* instead of first. |

The two layers answer different failures. **Retry** absorbs a blip on one model.
**Fallback** covers a model that stays unavailable for longer than the retries
last: retrying the same model harder does nothing when that model's capacity is
what is gone.

- **What moves the chain on:** `404` (a retired model does not come back), `408`,
  `429`, `5xx`, timeouts and dropped connections. **Never `400`**, which fails
  the same on every model, and never `401`/`403`, which are a configuration error.
- **Never mid-answer:** once a model has started answering, the turn stays on
  that model.
- **One family per chain:** ADK formats tool-call history differently for
  Gemini and for LiteLLM-routed models, so a chain that mixes them is refused
  at startup rather than failing on its first failover.
- **Cost of a dead primary:** retries run inside each link, so with a chain
  configured, keep `ORRERY_LLM_RETRY_ATTEMPTS` modest (2–3).
- **Observability:** `orrery_llm_failover_total{from_model,to_model}` counts
  failovers, and each one logs a `WARNING`. Evals run against the primary only,
  so keep chains within one capability tier.

### Billing labels (Vertex AI)

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_APP_LABEL` | `orrery` | `orrery_app` label on every Gemini request. Set one per install to split a shared GCP bill. |
| `ORRERY_ENVIRONMENT` | unset | `orrery_env` label (e.g. `prod`, `staging`). Omitted when unset. |
| `ORRERY_COST_LABELS` | `true` | `false` disables the labels. |

ADK already adds `adk_agent_name`, so billing splits per agent out of the box.
These labels add the deployment dimensions. Values are normalised to GCP's
label rules. The Gemini Developer API does not accept labels, and ADK strips
them there.

```bash
# Gemini Pro, falling back to Flash when Pro is unavailable
MODEL_NAME=gemini-3.6-pro
MODEL_FALLBACK_CHAIN=gemini-3.6-flash
ORRERY_LLM_RETRY_ATTEMPTS=3
```

---

## Session & Memory Persistence

Sessions (conversation state) and long-term memory (cross-session recall) share
one store. The platform supports exactly two backends — **in-memory** (no
`DATABASE_URL`) or **PostgreSQL** (`DATABASE_URL` set). SQLite is not supported.
Multi-replica deployments **require** PostgreSQL.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | unset | PostgreSQL URL, e.g. `postgresql+asyncpg://agents:secret@localhost:5432/agents`. When unset, sessions and memory are in-memory and lost on restart. |
| `ORRERY_DB_ALLOW_INMEMORY_FALLBACK` | `false` | **Local-dev only.** When `DATABASE_URL` is set but the DB is unreachable, allow degrading to in-memory instead of failing fast. |

!!! danger "Configured databases fail fast by design"
    If `DATABASE_URL` is set but PostgreSQL is unreachable at startup, the process
    **raises `DatabaseUnavailableError`** rather than silently running in-memory. A
    silent fallback in a multi-replica deployment would split sessions across pods
    and lose them on restart, while the pod still reported healthy. The crash keeps
    the pod in `CrashLoopBackOff` until the database is genuinely ready.

    For local development — where Postgres may not be running yet — set
    `ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1` to restore the graceful in-memory fallback.
    **Never set it in production.** The schema is created automatically on first use;
    no migration step is needed. See [Cross-session memory](../memory.md) and
    [Production deployment → Provision Postgres](../deployment.md#step-2-provision-postgres).

### Inspecting the store locally

`make up` starts **pgAdmin** on [http://localhost:5050](http://localhost:5050),
alongside Postgres and unprofiled for the same reason `kafka-ui` is — the thing
it inspects is always running. It opens straight into the browser tree with the
connection pre-registered (`infra/pgadmin/servers.json`): no sign-in page, no
master password. Enter the database password once on first connect
(`POSTGRES_PASSWORD`, default `agents_secret`) and tick **Save password**; it
persists in the `pgadmin-data` volume.

The tables worth knowing, all created by ADK:

| Table | Holds |
|---|---|
| `sessions` | One row per conversation — `state` carries `conversation_title`, `user_role`, `incident_severity`, the activity log. |
| `events` | Every turn's events: user messages, model replies, tool calls, compaction digests. The console's transcripts are rebuilt from here. |
| `app_states` / `user_states` | App- and user-scoped state, merged into a session's `state` on read. |
| `orrery_memory_events` | Long-term memory (`SecureMemoryService`), searched by `load_memory`. |

It binds to `127.0.0.1` only, and the login page is disabled because it sits in
front of a database whose password ships in `.env.example` — a second login to
reach it would buy nothing. Both facts stop being true the moment this is
exposed off-host, so don't: it is a local-development container.

---

## Context Caching

Context caching reduces token usage and latency by caching static system instructions (agent descriptions, tool schemas, RBAC rules) across requests. This is only effective with **Gemini models** — when using Claude/OpenAI via LiteLLM, the config is accepted but has no effect.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_CACHE_MIN_LENGTH` | `2048` | Only cache if context exceeds this token count |
| `CONTEXT_CACHE_TTL_SECONDS` | `600` | Cache lifetime in seconds (10 minutes) |
| `CONTEXT_CACHE_INTERVALS` | `10` | Max invocations before cache refresh |

Context caching is enabled by default in the `orrery-assistant` agent. You can tune the values via environment variables or disable it by not passing a `context_cache_config` to `run_persistent()`.

Cache hit/miss events are exposed as the `orrery_context_cache_events_total` Prometheus counter on the `/metrics` endpoint.

---

## Context Compaction

Caching shrinks what a request *costs*; compaction shrinks what it *contains*. Once a session's prompt grows past `ORRERY_COMPACTION_TOKEN_THRESHOLD`, ADK replaces the older turns with an LLM-written digest and keeps the most recent `ORRERY_COMPACTION_RETENTION_EVENTS` events verbatim.

Without it a long incident session grows monotonically until the request exceeds the model's window and the turn fails (`400 INVALID_ARGUMENT` on Gemini). `ToolOutputCapPlugin` caps a *single* tool result at 4 MiB, never the accumulated transcript — three capped results in history already approach the ~10 MiB request ceiling. Compaction is the only thing that bounds the conversation as a whole.

Compaction is **lossy for the model but lossless for the record**. ADK appends the digest as a new event carrying the compacted timestamp range; the original events stay in the session store and are filtered out only when assembling the request. Audit, replay, and `GET /session/{id}/activity` still see everything.

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_CONTEXT_COMPACTION` | `true` | Master switch; `false`/`0` disables compaction entirely |
| `ORRERY_COMPACTION_TOKEN_THRESHOLD` | `250000` | Compact once the last observed prompt reached this many tokens (`0` disables) |
| `ORRERY_COMPACTION_RETENTION_EVENTS` | `20` | Recent raw events kept verbatim |
| `ORRERY_COMPACTION_INTERVAL` | `50` | Sliding-window backstop: invocations before a turn-count compaction |
| `ORRERY_COMPACTION_OVERLAP` | `2` | Invocations of overlap between consecutive summaries |
| `ORRERY_COMPACTION_MODEL` | `gemini-flash-latest` | Model used to write the digest |

On by default across every transport. The 250k default is deliberately out of reach for ordinary sessions — enabling compaction should change nothing except for the long investigations it exists to rescue.

**Notes**

- **The summarizer runs on a cheap model.** Summarization is plumbing, not user-facing reasoning. Left unset, ADK would derive it from the root agent's own model and bill digests at that rate. On non-Gemini providers there is no cheap default we can assume exists, so `ORRERY_COMPACTION_MODEL` falls back to the agent's `MODEL_NAME` — set it explicitly to get the savings.
- **The sliding-window backstop cannot be switched off.** ADK requires `compaction_interval`/`overlap_size` whenever compaction is configured, so it always runs on invocations where the token threshold did not fire. The default interval is set high enough that the token trigger normally does the work.
- **Compaction invalidates the cached prefix** on the turn it fires, since the history it covers changed. Tune the threshold well below the model's hard limit but high enough that most sessions never compact, and watch the counter below against your cache hit rate.

Compactions are exposed as the `orrery_context_compaction_total` Prometheus counter on the `/metrics` endpoint.

---

## Agents

Which specialists the coordinator can delegate to, and how to add your own. See
[Adding an agent](../adding-an-agent.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_SPECIALISTS` | every built-in | Comma-separated entry-point keys to run (e.g. `kafka,k8s`). A third-party specialist runs only when named here. An unknown key stops startup. |
| `ORRERY_DISABLED_SPECIALISTS` | unset | Keys to leave out. |
| `ORRERY_DISABLED_TOOLS` | unset | Comma-separated `specialist.tool` names to take out of running specialists (e.g. `observability.get_alert_groups`). Only removes; an unknown name, a tool of a specialist that is not running, or one its health check, remediation, journal or self-test uses stops startup ([details](../specialist-selection.md#step-1-which-specialists-exist)). |
| `ORRERY_AGENTS_CONFIG` | unset | YAML file of [declarative agents](../adding-an-agent.md#declarative) built from existing tools. |
| `ORRERY_ADMIN_EDITING` | `false` | Let admins switch specialists and compose agents from the console ([details](../adding-an-agent.md#admin-editing)). |
| `ORRERY_CATALOG_POLL_SECONDS` | `15` | How often each replica checks for catalog edits made through another one. |
| `ORRERY_SHUTDOWN_GRACE_SECONDS` | `30` | On shutdown, how long turns whose clients left may finish, after uvicorn's `--timeout-graceful-shutdown` (15 in the image) has waited for connected ones. Keep the sum under the pod's termination grace period minus its preStop sleep. |
| `ORRERY_MCP_CONFIG` | unset | YAML file of [MCP servers](../integrations/mcp.md) to expose as gated specialists. Needs the `[mcp]` extra. |
| `ORRERY_MCP_MAX_RESPONSE_BYTES` | `8388608` | Most bytes one MCP HTTP response (or one SSE event) may hold; compressed responses are refused. Also applies to Studio registry MCP entries. |

## Process

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_MAX_WORKER_THREADS` | 8 per CPU of the cgroup quota, 8–64 | Size of the thread pool blocking tool calls run on. The default reads the container's CPU limit, not the host's core count. |
| `ORRERY_TURN_TIMEOUT_SECONDS` | `900` | Wall-clock ceiling for one turn on every surface; `0` disables. Tools and model calls have their own timeouts, so this only stops something wedged. A stopped turn is reported as "may already have acted" (HTTP 504 on `/chat`, an `error` event on `/chat/stream`). Kept above the Google Chat bot's own 600 s budget, whose notice lists what changed. |
| `LOG_LEVEL` | `INFO` | Log level for the JSON logs on stdout (`DEBUG`, `INFO`, `WARNING`, …). |

## HTTP front door

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_CHAT_RATE_LIMIT` | `30/minute` | Per-user limit on `/chat` and `/chat/stream` (one shared bucket). |
| `ORRERY_SELFTEST_RATE_LIMIT` | `10/minute` | Per-user limit on the environment self-test. |
| `ORRERY_RATE_LIMIT_STORAGE_URI` | `memory://` | Counter storage. `memory://` counts per replica; `redis://host:6379` shares counters. |
| `ORRERY_CONFIRMATION_BACKEND` | `memory` | Where pending approvals live: `postgres` (via `DATABASE_URL`) shares them across replicas. See [Guardrails](../guardrails.md). |
| `ORRERY_MULTI_REPLICA` | unset | Set by the Helm chart when more than one pod serves traffic. The server then refuses to start without `DATABASE_URL`, with a confirmation backend other than `postgres`, or with A2A enabled. |

A refused request is a `429` with `Retry-After`, which the console shows as "retry in Ns".

### Daily token budgets

A rate limit bounds how often a turn starts, not what it costs: one message can
fan out to every specialist, and each model call re-sends the run so far. Two
daily ceilings bound the bill. Both are off by default, are counted per UTC day
from each model response's token usage (prompt, output and thinking), and apply
on every transport, since they are a plugin in `default_plugins()`.

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_DAILY_TOKEN_BUDGET_PER_USER` | `0` (off) | Tokens one caller may spend per day. The caller is the verified subject; an agent acting for a user spends that user's budget. |
| `ORRERY_DAILY_TOKEN_BUDGET` | `0` (off) | Tokens the whole deployment may spend per day, whoever spends them. |

Once a budget is spent, model calls are answered with a short "usage limit
reached, resets at 00:00 UTC" reply instead of being sent, so the refusal costs
nothing. Each call reserves an estimate of its prompt before it goes out
(characters / 4), so calls started at the same moment see each other, and the
response's reported usage replaces the estimate afterwards. A budget can still
be exceeded by the output of the calls already in flight and by the estimate's
error. Counters use `ORRERY_RATE_LIMIT_STORAGE_URI`: per process with
`memory://` (they reset on restart, and with `ORRERY_MULTI_REPLICA` each
replica counts its own, which startup warns about), shared with `redis://`.
Refusals are counted in `orrery_token_budget_refusals_total{scope="user"|"global"}`.

The store is a cost control, not a security gate: if it fails or takes longer
than 2 s, the call goes ahead unbudgeted, the error is logged, and
`orrery_token_budget_store_errors_total` counts it. `OrreryTokenBudgetStoreFailing`
(`infra/alert_rules.yml`) fires on it.

Who is charged:

- Console and A2A: the verified subject. Slack: the Slack user id. Google Chat:
  the verified email. An agent acting for a user, and every specialist a turn
  calls, spends that user's budget.
- With authentication off, the console keys on `anonymous:<client IP>`, which
  a caller can rotate and which everyone behind one proxy shares; only the
  deployment budget means much there.
- The scheduled triage sweep runs as `batch`, so it draws on the deployment
  budget like everyone else: once that is spent, a sweep's nodes are refused.

Not charged: the compaction summarizer, knowledge embeddings, the onboarding
self-test probe (all called outside the plugin chain), and calls that fail or
are cancelled before their final response.

A2A exposition (`ORRERY_A2A_*`) has its own table in [A2A](../integrations/a2a.md#enabling-it).

### Chat attachments

Images and UTF-8 text files on `/chat` and `/chat/stream` (multipart), processed in memory for one
turn and never stored ([AEP-030](../enhancements/aep-030-chat-attachments.md)).

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_ATTACHMENTS` | `true` | Master switch. Off: `/me` reports `attachments: null` and multipart bodies get `415`. |
| `ORRERY_ATTACHMENT_IMAGES` | `true` | Accept images. Set `false` for a model without vision. |
| `ORRERY_ATTACHMENT_MAX_FILES` | `4` | Files per message. |
| `ORRERY_ATTACHMENT_MAX_BYTES` | `10485760` | Per file, as uploaded (10 MiB). |
| `ORRERY_ATTACHMENT_MAX_TOTAL_BYTES` | `25165824` | Whole request body (24 MiB). A proxy in front must allow it (nginx `client_max_body_size`). |
| `ORRERY_ATTACHMENT_MAX_PIXELS` | `25000000` | Source-image budget, checked from the header before decoding. |
| `ORRERY_ATTACHMENT_IMAGE_MAX_EDGE` | `1568` | Long edge after downscaling. |
| `ORRERY_ATTACHMENT_MAX_TEXT_CHARS` | `200000` | Characters of a text file the model sees; the middle is cut beyond this. |
| `ORRERY_ATTACHMENT_MAX_CONCURRENT_DECODES` | `2` | Image decodes at once, per process (bounds peak memory). |
| `ORRERY_ATTACHMENT_MAX_CONCURRENT_UPLOADS` | `4` | Multipart bodies held in memory at once, per process. Past it a request waits, then gets `503` with `Retry-After`. |
| `ORRERY_ATTACHMENT_MAX_UPLOADS_PER_USER` | `1` | Multipart bodies one caller may have in flight. Past it the request gets `429` with `Retry-After`, before it takes a process-wide slot. |
| `ORRERY_ATTACHMENT_READ_TIMEOUT_SECONDS` | `60` | Deadline for receiving a request body (`408` after). |

A proxy in front must not spool bodies to disk: nginx `proxy_request_buffering off`, or on
Ingress-NGINX `nginx.ingress.kubernetes.io/proxy-request-buffering: "off"` (the Helm chart's default
ingress annotations set it, with `proxy-body-size: 25m`). For a 1 GiB container use
`MAX_CONCURRENT_DECODES=1`, `MAX_PIXELS=16000000`, `MAX_CONCURRENT_UPLOADS=2`.

With attachments on, the server defaults ADK's `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` to `false`
(so does `configure_tracing`), so prompts, uploaded text and tool results stay out of trace spans.
Set it to `true` explicitly to opt back in.

## Distributed Tracing

OpenTelemetry tracing follows a single request *through* the agent hierarchy so you can attribute latency and localize failures. It is an opt-in extra (`uv sync --extra otel`), **off by default**, and driven entirely by the env vars below — `default_plugins()` reads `OTEL_TRACING_ENABLED` automatically, so one flag turns it on across every transport.

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_TRACING_ENABLED` | `false` | Master switch. Must be `true` for any tracing to occur. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP gRPC collector endpoint (e.g. `http://localhost:4317`). When empty, spans print to the console — useful for local debugging. |
| `OTEL_SERVICE_NAME` | `orrery` | `service.name` resource attribute attached to every span. |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Head-sampling ratio `0.0`–`1.0`. Uses a `ParentBased` sampler, so a trace already sampled upstream is always kept. |

!!! tip "Full guide → [Platform Features → Observability](../metrics.md#distributed-tracing)"
    The Observability page covers what the spans look like, the `make up PROFILES=tracing` local Tempo + Grafana stack, the provisioned dashboard, and log↔trace correlation.

---

## Planning

The reasoning-heavy agents in `orrery-assistant` (the root orchestrator, `triage_summarizer`, and `remediation_actor`) accept an optional [ADK planner](https://adk.dev/agents/llm-agents/) that injects an explicit reasoning step before tool calls. Planning is **opt-in** and **off by default** — setting `ORRERY_PLANNER` is the only knob you need.

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_PLANNER` | `none` | Planner choice: `none`, `plan_react`, or `builtin`. |
| `ORRERY_PLANNER_THINKING_BUDGET` | unset | Integer token budget for `builtin` (Gemini-only). |
| `ORRERY_PLANNER_INCLUDE_THOUGHTS` | `true` | Whether `builtin` surfaces the model's thoughts. |

**Choosing a planner:**

- `plan_react` is **provider-agnostic** — works with every backend `MODEL_PROVIDER` supports (Gemini, Claude, OpenAI, Ollama via LiteLLM). The model output is structured into `/*PLANNING*/`, `/*ACTION*/`, `/*REASONING*/`, and `/*FINAL_ANSWER*/` phases. Use this if you run on anything other than Gemini, or if you want plan steps you can surface in a UI (the Google Chat progress card already keys off `state_delta` writes, so the additional structure shows up naturally).
- `builtin` uses **Gemini's native thinking tokens** via `BuiltInPlanner(ThinkingConfig(...))`. Cheaper and lower-latency than `plan_react` on Gemini, with no output-shape change. Falls back to no planner (with a warning) when `MODEL_PROVIDER != gemini`, since LiteLLM-routed models do not consume the ADK thinking config.

**Tradeoffs to be aware of:**

- `plan_react` makes responses noticeably more verbose and adds an extra reasoning round-trip per turn — A/B-test before flipping it on for latency-sensitive surfaces.
- Planners are intentionally **not** attached to per-system health checkers, the remediation verifier, or the journal writer. Those agents execute one short tool sequence per turn; planning would add latency without changing the output.
- **Reasoning never leaks to users.** Whatever planner you choose, the model's reasoning (`builtin` thought tokens, `plan_react` `/*PLANNING*/`/`/*REASONING*/` phases, and provider reasoning surfaced through LiteLLM) is marked as a *thought* part by ADK. Every user-facing transport — Google Chat, Slack, the HTTP `/chat` API, and the CLI — funnels event text through `extract_reply_text()` (`orrery_core.serving.events`), which drops thought parts and emits only the final answer. So `ORRERY_PLANNER_INCLUDE_THOUGHTS=true` makes thoughts available for tracing/progress without showing them in the reply.

---

## Infrastructure

The included `docker-compose.yml` starts the local diagnostic stack.

| Service | Port | Description |
|---------|------|-------------|
| Kafka | `9092` | Kafka broker (running in KRaft mode) |
| PostgreSQL | `5432` | Shared session storage for agents |
| pgAdmin | `127.0.0.1:5050` | Database browser for the session store (loopback only) |
| Kafka UI | `8080` | Web UI for browsing topics and consumer groups |
| Kafka Exporter | `9308` | Prometheus exporter for Kafka metrics |
| Prometheus | `9090` | Metrics collection and alerting rules |
| Loki | `3100` | Log aggregation |
| Alertmanager | `9093` | Alert routing and silence management |
| Tempo | `4317` / `3200` | OTLP span ingest + Tempo query API (`tracing` profile) |
| Grafana | `3001` | Trace/metric visualization (`tracing` profile) |
| Keycloak | `8081` | Local identity provider, realm `orrery` (`sso` profile) |
| Elasticsearch | `9200` | Elasticsearch REST endpoint (`elastic` profile) |
| Kibana | `5601` | Kibana web UI (`elastic` profile) |

`make up` starts every profile listed here; `make up PROFILES=…` narrows it.

### Management Commands

```bash
make up                   # start all services
make down                 # stop all services
make reset                # stop and wipe volumes
make up PROFILES=tracing  # start the tracing stack (Tempo + Grafana)
make down # stop the tracing stack
```

### Docker Compose profiles

| Command | What it starts |
|---------|---------------|
| `docker compose up -d` | Infrastructure only |
| `docker compose --profile demo up -d` | Infrastructure + orrery-assistant web UI on `:8000` |
| `docker compose --profile slack up -d` | Infrastructure + Slack bot on `:3000` |
| `docker compose --profile tracing up -d` | Tempo (OTLP `:4317`) + Grafana (`:3001`) |
| `docker compose --profile elastic up -d` | Elasticsearch + Kibana |
| `docker compose --profile sso up -d` | Keycloak on `:8081` (for `make run-api SSO=1`) |
