# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
make install          # Install all workspace packages (uv sync)
make test             # Run all 237 tests across all packages
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

- **`core/`** — Shared library (`ai-agents-core`): agent factory, RBAC, config, guardrails, structured logging, audit trail, activity tracking, error handlers, persistent runner
- **`agents/`** — Independent agent packages, each runnable standalone or composable:
  - `kafka-health/` — Kafka cluster monitoring (8 tools, uses confluent-kafka)
  - `k8s-health/` — Kubernetes cluster management (11 tools, uses kubernetes client)
  - `ops-journal/` — State management demo with 4 state scopes (session/user/app/temp)
  - `devops-assistant/` — Multi-agent orchestrator that composes all above agents + Docker tools

### Key Design Patterns

- **Callbacks over inheritance**: Safety (guardrails), logging (audit), activity tracking, and error handling are plugged in via callbacks passed to `create_agent()`, not through subclassing.
- **Agent factory functions** in `core/ai_agents_core/base.py`: `create_agent()`, `create_sequential_agent()`, `create_parallel_agent()`.
- **Output keys for data flow**: In multi-agent workflows (like `devops-assistant`), sub-agents write results to session state via `output_key`; downstream agents read them.
- **RBAC via guardrail metadata**: `authorize()` in `core/ai_agents_core/rbac.py` infers minimum roles from `@destructive`/`@confirm` decorators (admin/operator/viewer). User role is read from `session.state["user_role"]`. Composes with guardrails: `before_tool_callback=[authorize(), require_confirmation()]`. See `docs/adr/001-rbac.md`.
- **Guardrails as decorators**: `@destructive(reason)` and `@confirm(reason)` attach metadata to tool functions. `require_confirmation()` / `dry_run()` callbacks read this metadata at runtime.
- **Structured JSON logging**: `setup_logging()` configures JSON output to stdout (called automatically by `load_agent_env()`). `audit_logger()` emits tool-call audit entries via the logging system. `activity_tracker()` records tool calls to session state for cross-agent visibility.
- **Connection pooling**: Kafka `AdminClient`, K8s API clients, and HTTP sessions are cached as module-level singletons to avoid per-call connection overhead.
- **Pydantic-settings config**: Each agent subclasses `AgentConfig` for typed env var loading from `.env` files colocated with the agent module.
- **All tests use mocks**: `@patch` on internal client getters (e.g., `_get_admin_client`). No running Kafka/K8s/Docker required. Autouse fixtures reset cached clients between tests.

### devops-assistant Agent Hierarchy

```
devops_assistant (root orchestrator)
├── incident_triage_agent (SequentialAgent)
│   ├── health_check_agent (ParallelAgent)
│   │   ├── kafka_health_checker
│   │   ├── k8s_health_checker
│   │   ├── docker_health_checker
│   │   └── observability_health_checker
│   ├── triage_summarizer
│   └── journal_writer
├── kafka_health_agent
├── k8s_health_agent
├── observability_agent
├── docker_agent (5 tools using subprocess Docker CLI)
└── ops_journal_agent
```

## Code Style

- **Ruff** for linting and formatting (line-length: 100, target: py311)
- Lint rules: E, W, F, I (isort), UP, B, SIM
- Known first-party packages configured in `[tool.ruff.lint.isort]`
- CI runs both `ruff check` and `ruff format --check`
