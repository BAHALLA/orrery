# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-04-11

### Added

- **Google Chat Bot Integration**: A new integration bringing autonomous DevOps agents to Google Workspace with support for thread-based sessions and interactive Card v2.
- **Workspace Add-ons Compatibility**: Implemented strict `hostAppDataAction` (DataActions) schema support, enabling the bot to run behind the Google Workspace Add-ons pipeline.
- **Dual-Path Event Detection**: Added logic to seamlessly handle interaction events from both standard Google Chat API and the nested Workspace Add-ons event structure.
- **Interactive Guardrails for Chat**: Wired `@confirm` and `@destructive` tools to post interactive Cards v2 with Approve/Deny buttons, allowing operators to authorize dangerous actions directly from the chat.
- **Configurable Identities**: Added `GOOGLE_CHAT_IDENTITIES` to allow dynamic verification of multiple signing service accounts (e.g., standard Chat vs Add-ons service agents).
- **Kafka KRaft Migration**: Removed Zookeeper dependency. Kafka now runs in KRaft mode for improved startup reliability and simplified architecture.
- **PostgreSQL Service**: Added a dedicated PostgreSQL container to `docker-compose.yml` for persistent session storage.
- **Centralized Configuration**: Merged per-agent `.env` files into a single root `.env` file. Updated core library to prioritize the root configuration while maintaining legacy override support.
- **Cross-Session Memory**: Enabled `MemoryPlugin` in the `devops-assistant` agent, allowing it to remember past interactions and save session highlights to the persistent store.
- **Kafka Partition Scaling**: Added `update_kafka_partitions` tool to the Kafka health agent with full unit test coverage.
- **Production deployment hardening** (AEP-011) — complete Kubernetes deployment story
  - Kustomize manifests under `deploy/k8s/` (Deployment, Service, HPA, PDB, NetworkPolicy, ServiceAccount with scoped ClusterRoles)
  - Helm chart under `deploy/helm/devops-assistant/` with configurable values, NOTES, and `existingSecret` support for out-of-band secret management
  - GHCR CD pipeline (`.github/workflows/docker-publish.yml`) publishing multi-arch (amd64/arm64) images with SBOM and provenance attestation on merges to `main` and `v*.*.*` tags
  - PostgreSQL session store support — `runner.py` honors `DATABASE_URL` (async driver `postgresql+asyncpg://…`) for multi-instance deployments; `core[postgres]` extra adds `asyncpg` and `psycopg2-binary`
  - Rate limiting on the Slack bot `/slack/events` webhook via `slowapi` (configurable via `SLACK_RATE_LIMIT`, default `60/minute`)
  - Root-level `.env.example` documenting every required and optional variable across agents and deployment manifests
  - `docs/deployment.md` — end-to-end production deployment guide (Postgres setup, Helm install, rolling updates, troubleshooting)

### Changed

- `SlackBotConfig.resolve_db_url()` prefers `DATABASE_URL` env var over the legacy `slack_db_url` default — enables sharing Postgres between the Slack bot and the ADK web UI workloads
- `load_agent_env()` and `load_config()` now search for a `.env` file at the project root by default.
- Enhanced `docs/integrations/google-chat.md` with a detailed Workspace Add-ons setup guide and visual demos.

### Fixed

- **OIDC Token Verification**: Fixed a bug where tokens from Google's migrated OIDC flow (`iss=accounts.google.com`) were rejected. Added proper identity verification against the `email` claim.
- **Dynamic Session Handling**: Fixed `SessionNotFoundError` in the ADK Runner by enabling `auto_create_session=True` for thread-based chat integrations.
- **JSON Schema Validation**: Resolved "Failed to parse JSON" errors in Google Chat by ensuring all responses (including errors and added-to-space events) strictly follow the Add-ons response schema.
- **Metrics Callback Signature**: Fixed a `TypeError` in `MetricsPlugin` where incorrect keyword arguments were passed to the internal callback.
- **Kafka Tool Imports**: Fixed a `NameError` in `kafka_health_agent/tools.py` caused by using decorators before they were imported.
- **Database URL Masking**: Ensured `DATABASE_URL` is masked in all log outputs and console prints to prevent credential leaks.

## [0.1.0] - 2026-04-09

First public release of the AI Agents for DevOps & SRE platform.

### Added

- **Multi-agent orchestrator** — `devops-assistant` root agent delegates to 5 specialist agents via `AgentTool` and deterministic sub-agent workflows ([ADR-002](docs/adr/002-agent-tool-vs-sub-agents.md))
- **Specialist agents** — Kafka health, K8s health, Observability (Prometheus/Loki/Alertmanager), Docker, and Ops Journal
- **Slack bot** — Thread-based sessions with interactive Approve/Deny buttons for guarded operations
- **Incident triage pipeline** — `SequentialAgent` + `ParallelAgent` for parallel health checks across all systems, triage summary, and journal recording
- **Closed-loop remediation** (AEP-004) — `LoopAgent`-based pipeline: act (restart/scale/rollback) → verify → retry up to 3 iterations, with `exit_loop` tool for early termination
- **Context caching** (AEP-007) — ADK `ContextCacheConfig` for Gemini models, reducing token usage for repeated requests. Configurable via `CONTEXT_CACHE_MIN_TOKENS`, `CONTEXT_CACHE_TTL_SECONDS`, `CONTEXT_CACHE_INTERVALS` env vars
- **Cross-session memory** (AEP-003) — `SecureMemoryService` with automatic PII redaction and size limits
- **Agent evaluation framework** (AEP-002) — 22 eval scenarios across 4 agents verifying correct tool routing via ADK's `AgentEvaluator`. Run with `make eval`
- **RBAC** — 3-role hierarchy (viewer/operator/admin) enforced globally via `GuardrailsPlugin` ([ADR-001](docs/adr/001-rbac.md))
- **Safety guardrails** — `@destructive` and `@confirm` decorators gate dangerous operations with args-hash + TTL confirmation tracking (AEP-001)
- **Authentication enforcement** — `set_user_role()` marks server-trusted roles; `ensure_default_role()` forces `viewer` for unset roles
- **Input validation** — 5 reusable validators (`validate_string`, `validate_positive_int`, `validate_url`, `validate_path`, `validate_list`) applied across 30+ tool functions
- **ADK Plugins** — cross-cutting concerns as `BasePlugin` subclasses: `GuardrailsPlugin`, `ResiliencePlugin`, `MetricsPlugin`, `AuditPlugin`, `ActivityPlugin`, `ErrorHandlerPlugin`, `MemoryPlugin`
- **Prometheus metrics** — tool call counts, latency histograms, error rates, circuit breaker state, LLM tokens, and context cache events on `/metrics`
- **Resilience** — per-tool circuit breaker via `ResiliencePlugin`, `@with_retry` decorator with exponential backoff and jitter
- **Structured JSON logging** — `setup_logging()` with `JSONFormatter`, audit trail via `AuditPlugin`, activity tracking via `ActivityPlugin`
- **Multi-provider LLM** — Gemini (default), Claude, OpenAI, Ollama via `resolve_model()` + LiteLLM
- **Persistent runner** — `run_persistent()` with SQLite-backed sessions, health probes, graceful shutdown
- **Agent factory functions** — `create_agent()`, `create_sequential_agent()`, `create_parallel_agent()`, `create_loop_agent()`
- **Docker deployment** — multi-stage builds, non-root user, `docker-compose.yml` with demo/slack profiles
- **468 unit tests** — all async, all mocked, no running infrastructure required
- **CI pipeline** — lint (ruff), type check (ty), security scan (bandit), tests, evals

### Security

- Input validation at tool boundaries prevents injection attacks
- Path traversal prevention in Docker and K8s tools
- URL scheme allowlisting rejects `javascript:`, `data:`, `file:` URIs
- Docker container inspection redacts sensitive environment variables
- Guardrail confirmation bypass fixed with args-hash + TTL tracking
- Server-side role enforcement prevents privilege escalation

[Unreleased]: https://github.com/BAHALLA/devops-agents/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/BAHALLA/devops-agents/releases/tag/v0.1.0
