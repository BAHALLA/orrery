# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
make install          # Install all workspace packages (uv sync)
make test             # Run all 404 tests across all packages
make lint             # ruff check + format check
make fmt              # Auto-fix linting and formatting
```

Run tests for a single agent:
```bash
uv run pytest agents/kafka-health/tests/ -v
```

Run infrastructure (Kafka + Zookeeper + Kafka UI):
```bash
make infra-up         # Start infrastructure
make infra-down       # Stop infrastructure
make infra-reset      # Stop + wipe volumes (fixes cluster.id mismatch)
```

Docker demo (full stack with web UI on :8000):
```bash
make docker-build && make docker-demo
```

Run individual agents (each has `make run-<name>`, `make run-<name>-cli`, some have `-persistent`):
```bash
make run-devops       # ADK Dev UI (in-memory)
make run-devops-cli   # Terminal mode
```

## Architecture

This is a **DevOps/SRE agent platform** built on **Google ADK** (Agent Development Kit). It uses a **uv workspace** with Python 3.11.

### Workspace Layout

- **`core/`** — Shared library (`ai-agents-core`): agent factory, multi-provider LLM support (Gemini/Claude/OpenAI/Ollama via LiteLLM), RBAC, config, guardrails, input validation, resilience (circuit breaker + retry), structured logging, audit trail, activity tracking, error handlers, persistent runner
- **`agents/`** — Independent agent packages, each runnable standalone or composable:
  - `kafka-health/` — Kafka cluster monitoring (8 tools, uses confluent-kafka)
  - `k8s-health/` — Kubernetes cluster management (11 tools, uses kubernetes client)
  - `ops-journal/` — State management demo with 4 state scopes (session/user/app/temp)
  - `devops-assistant/` — Multi-agent orchestrator that composes all above agents + Docker tools

### Key Design Patterns

- **Plugins over per-agent callbacks**: Cross-cutting concerns (RBAC, guardrails, metrics, audit, activity tracking, resilience, error handling) are packaged as ADK `BasePlugin` subclasses in `core/ai_agents_core/plugins.py` and registered once on the `Runner` via `default_plugins()`. Plugins apply globally to every agent, tool, and LLM call — no per-agent callback wiring needed.
- **Async tools**: All tool functions are `async def` and use `asyncio.to_thread()`, `asyncio.create_subprocess_exec()`, or `_run_sync()` to offload blocking I/O (Kafka, K8s, Docker, HTTP) to thread pool executors.
- **Agent factory functions** in `core/ai_agents_core/base.py`: `create_agent()`, `create_sequential_agent()`, `create_parallel_agent()`.
- **Output keys for data flow**: In multi-agent workflows (like `devops-assistant`), sub-agents write results to session state via `output_key`; downstream agents read them.
- **RBAC via guardrail metadata**: `authorize()` in `core/ai_agents_core/rbac.py` infers minimum roles from `@destructive`/`@confirm` decorators (admin/operator/viewer). User role is read from `session.state["user_role"]`. Enforced globally via `GuardrailsPlugin`. See `docs/adr/001-rbac.md`.
- **Input validation**: `core/ai_agents_core/validation.py` provides `validate_string()`, `validate_positive_int()`, `validate_url()`, `validate_path()`, `validate_list()` — all tools validate inputs at entry using the walrus operator pattern: `if err := validate_string(...): return err`.
- **Guardrails as decorators**: `@destructive(reason)` and `@confirm(reason)` attach metadata to tool functions. `GuardrailsPlugin` reads this metadata at runtime. Confirmations use args-hash + TTL to prevent bypass.
- **Authentication enforcement**: `set_user_role()` marks roles as server-trusted. `GuardrailsPlugin` calls `ensure_default_role()` via `before_agent_callback` to force `viewer` if the role wasn't set by the server, preventing privilege escalation.
- **Structured JSON logging**: `setup_logging()` configures JSON output to stdout (called automatically by `load_agent_env()`). `AuditPlugin` emits tool-call audit entries via the logging system. `ActivityPlugin` records tool calls to session state for cross-agent visibility.
- **Connection pooling**: Kafka `AdminClient`, K8s API clients, and HTTP sessions are cached as module-level singletons to avoid per-call connection overhead.
- **Multi-provider LLM**: `resolve_model()` in `core/ai_agents_core/base.py` reads `MODEL_PROVIDER` + `MODEL_NAME` env vars. For Gemini returns a string; for others returns `LiteLlm(model=...)`. All agents use this via `create_agent()` — no per-agent changes needed.
- **Prometheus metrics**: `MetricsPlugin` in `core/ai_agents_core/plugins.py` wraps `MetricsCollector` to track tool call counts, latency histograms, error rates, circuit breaker state, and LLM tokens globally. `start_server(port=9100)` exposes `/metrics` for Prometheus scraping.
- **Resilience**: `ResiliencePlugin` in `core/ai_agents_core/plugins.py` wraps `CircuitBreaker` for per-tool circuit breaking globally. `@with_retry` decorator adds exponential backoff with jitter to async tool functions.
- **Pydantic-settings config**: Each agent subclasses `AgentConfig` for typed env var loading from `.env` files colocated with the agent module.
- **All tests use mocks**: `@patch` on internal client getters (e.g., `_get_admin_client`). All tool tests are `async` with `@pytest.mark.asyncio`. No running Kafka/K8s/Docker required. Autouse fixtures reset cached clients between tests.

### devops-assistant Agent Hierarchy

Uses two delegation patterns (see [ADR-002](docs/adr/002-agent-tool-vs-sub-agents.md)):
- **Sub-agents** for deterministic workflows (SequentialAgent/ParallelAgent)
- **AgentTool** for LLM-routed specialist agents

```
devops_assistant (root orchestrator)
├── [sub-agent] incident_triage_agent (SequentialAgent)
│   ├── health_check_agent (ParallelAgent)
│   │   ├── kafka_health_checker
│   │   ├── k8s_health_checker
│   │   ├── docker_health_checker
│   │   └── observability_health_checker
│   ├── triage_summarizer
│   └── journal_writer
├── [AgentTool] kafka_health_agent
├── [AgentTool] k8s_health_agent
├── [AgentTool] observability_agent
├── [AgentTool] docker_agent (5 tools using subprocess Docker CLI)
└── [AgentTool] ops_journal_agent
```

## Code Style

- **Ruff** for linting and formatting (line-length: 100, target: py311)
- Lint rules: E, W, F, I (isort), UP, B, SIM
- Known first-party packages configured in `[tool.ruff.lint.isort]`
- CI runs both `ruff check` and `ruff format --check`
