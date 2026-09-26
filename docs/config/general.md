# General Configuration

The platform's core behavior, including LLM providers and infrastructure services, is controlled via environment variables.

## LLM Provider

The platform supports multiple LLM providers through [LiteLLM](https://docs.litellm.ai/). Switch providers by setting two environment variables — no code changes needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PROVIDER` | `gemini` | LLM backend: `gemini`, `anthropic`, `openai`, `ollama`, etc. |
| `MODEL_NAME` | `gemini-3.6-flash` | Model identifier (provider prefix auto-added if missing) |

### Provider examples

<details>
<summary>Google Gemini (Default)</summary>

```env
MODEL_PROVIDER=gemini
MODEL_NAME=gemini-2.5-pro
# Either Vertex AI:
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
# Or AI Studio:
GOOGLE_GENAI_USE_VERTEXAI=FALSE
GOOGLE_API_KEY=your-api-key
```
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
| Kafka UI | `8080` | Web UI for browsing topics and consumer groups |
| Kafka Exporter | `9308` | Prometheus exporter for Kafka metrics |
| Prometheus | `9090` | Metrics collection and alerting rules |
| Loki | `3100` | Log aggregation |
| Alertmanager | `9093` | Alert routing and silence management |
| Tempo | `4317` / `3200` | OTLP span ingest + Tempo query API (`tracing` profile) |
| Grafana | `3001` | Trace/metric visualization (`tracing` profile) |
| Elasticsearch | `9200` | Elasticsearch REST endpoint |
| Kibana | `5601` | Kibana web UI |

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
