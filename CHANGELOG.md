# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Elasticsearch Agent** (`agents/elasticsearch`): New specialist agent for Elasticsearch cluster operations, exposing **19 read-only REST tools** — cluster health/stats/nodes/pending tasks/settings, indices listing/stats/mappings/settings, shard allocation + `explain` diagnostics, `search` + `count`, index templates, aliases, ILM policies + `explain_ilm_status`, and snapshot repositories/snapshots. HTTP session is pooled as a module-level singleton with API-key / basic-auth / CA bundle support via `ELASTICSEARCH_*` env vars.
- **ECK Operator Tools**: Five Kubernetes control-plane tools complementing the REST surface — `list_eck_clusters`, `describe_eck_cluster`, `list_kibana_instances`, `describe_kibana`, `get_eck_operator_events`. Wired through the shared `orrery_core.default_registry.ECKDetector` for interpreted `healthy` / `phase` / `warnings` on each CR.
- **Orrery-assistant integration**: `elasticsearch_agent` is now a sibling `AgentTool` on the root orchestrator, and a new `elasticsearch_health_checker` joins `health_check_agent` as the fifth parallel branch of the incident triage pipeline (writes `elasticsearch_status` to session state for the `triage_summarizer`).
- **Compose profile + Makefile targets**: `docker-compose.yml` gains an `elastic` profile with single-node Elasticsearch 8.13.4 + Kibana (security disabled for dev); `make run-elasticsearch` / `make run-elasticsearch-cli` launch the agent standalone.
- **Tests & evals**: 36 new unit tests (25 REST + 11 ECK) with mocked `requests.Session` / `CustomObjectsApi` / `CoreV1Api`, plus 6 new eval scenarios (`cluster_and_indices.test.json`, `eck.test.json`) — bringing the suite to **608 tests / 28 eval scenarios**.

## [0.1.7] - 2026-04-19

### Added
- **Zero-clone Docker quick-start**: The README and `docs/getting-started.md` now lead with `docker pull ghcr.io/bahalla/orrery:latest` + `docker run` for a 30-second single-container test drive, followed by a `curl`-the-compose-file path for the full Kafka/Postgres/Prometheus stack — no repository clone required.
- **`ORRERY_IMAGE` override**: `docker-compose.yml` honours `ORRERY_IMAGE` so users can pin a specific release tag (e.g. `ORRERY_IMAGE=ghcr.io/bahalla/orrery:v0.1.7 docker compose --profile demo up -d`).
- **Best-practices & scaling guide**: `docs/agent-design-patterns.md` gains three new sections — tool/agent sizing budgets (sweet 5–15 tools, 3–7 direct children, depth ≤3), framework/model-specific limits (Gemini/Claude/OpenAI/Ollama caps, ADK `LoopAgent.max_iterations` discipline, context-window budgeting), and a decision guide for when to reach for the A2A protocol (referencing [AEP-005](docs/enhancements/aep-005-a2a-protocol.md)) with a four-stage scaling playbook.

### Changed
- **Python 3.14 upgrade**: Bumped `requires-python` from `>=3.11` to `>=3.14` across the root and all nine workspace packages (`core`, `docker-agent`, `google-chat-bot`, `k8s-health`, `kafka-health`, `observability`, `ops-journal`, `orrery-assistant`, `slack-bot`). Ruff `target-version` updated to `py314`. Dockerfile base images switched to `ghcr.io/astral-sh/uv:python3.14-bookworm-slim` (builder) and `python:3.14-slim-bookworm` (runtime). CI and release workflows now install Python 3.14. `uv.lock` regenerated — all C-extension wheels (confluent-kafka, psycopg2-binary, asyncpg, pydantic-core, numpy, tiktoken) resolved to prebuilt 3.14 wheels with no source-build fallbacks. Full test suite (572 tests) passes on 3.14.
- **Single production Dockerfile**: Merged `Dockerfile.prod` into `Dockerfile` so the repository ships one production-ready image. The consolidated Dockerfile adds `UV_COMPILE_BYTECODE=1`, `UV_LINK_MODE=copy`, `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`, a BuildKit `--mount=type=cache` for uv, `--extra postgres` by default, and folds ownership into `COPY --chown=` to drop a redundant `chown -R` layer.
- **Compose services pull by default**: `orrery-assistant` and `slack-bot` in `docker-compose.yml` now use `image: ${ORRERY_IMAGE:-ghcr.io/bahalla/orrery:latest}` with `build: .` retained as a local-dev fallback — first-run users no longer wait for a local build.
- **CI/release type-check coverage**: `.github/workflows/ci.yml` and `.github/workflows/release.yml` now include `--extra-search-path agents/google-chat-bot` in the `ty` invocation so that agent is type-checked alongside the others (it was already shipping in the image and in runtime CI).
- **Documentation refresh**: `SECURITY.md`, `docs/deployment.md`, `docs/troubleshooting.md`, `docs/enhancements/aep-011-deployment-hardening.md`, and `docs/enhancements/aep-014-supply-chain-security.md` updated to reference the single `Dockerfile` after the consolidation.

### Fixed
- **Broken `HEALTHCHECK` on the default image**: The Dockerfile `HEALTHCHECK` and the `orrery-assistant` compose healthcheck both probed `http://localhost:8080/healthz`, but `HealthServer` is only started by `run_persistent()` and the Pub/Sub worker — not by the default `adk web` CMD. Probes are now owned by the orchestrator (docker-compose / Helm values in `deploy/helm/orrery-assistant/values.yaml`) rather than baked into the image.
- **Latent port conflict in compose**: `orrery-assistant` and `kafka-ui` both bound host port `8080`. The unused `8080:8080` mapping on `orrery-assistant` was removed (the healthz server doesn't run for that CMD anyway).

### Removed
- **`Dockerfile.prod`**: replaced by the consolidated, production-ready `Dockerfile`. The `release.yml` workflow now builds from `./Dockerfile`.

## [0.1.6] - 2026-04-19

### Added
- **Operator Registry (`orrery_core.operators`)**: Pluggable registry for Kubernetes operator detection and CR status interpretation. Ships with built-in detectors for **Strimzi** (`kafka.strimzi.io` — 9 kinds incl. `Kafka`, `KafkaTopic`, `KafkaConnector`, `KafkaRebalance`) and **ECK** (`*.k8s.elastic.co` — 7 kinds incl. `Elasticsearch`, `Kibana`, `ApmServer`, `Beat`). New detectors can be registered via `default_registry.register()`.
- **Structured Tool Results (`orrery_core.ToolResult`)**: Pydantic model with `ok()` / `error()` / `partial()` factories and `remediation_hints` for cross-agent composition. Flattens to a backward-compatible dict via `.to_dict()`, so adoption is gradual and existing tests/tools keep working.
- **k8s-health Operator-Aware Tools**: Six new tools on the `k8s-health` agent — `detect_operators`, `list_custom_resources`, `describe_custom_resource`, `get_owner_chain`, `describe_workload`, `get_operator_events`. `describe_workload` walks `ownerReferences` from a Pod up to its root CR (e.g., Pod → StatefulSet → `Kafka`) and returns the operator's interpreted status (healthy/phase/warnings) instead of raw pod info.
- **kafka-health Strimzi Tools**: Ten new tools that complement the Kafka-protocol tools with a view into the Strimzi control plane — `list_strimzi_clusters`, `describe_strimzi_cluster`, `list_strimzi_topics`, `list_kafka_users`, `list_kafka_connectors`, `get_kafka_connect_status`, `get_mirrormaker2_status`, `get_kafka_rebalance_status`, plus the guarded `approve_kafka_rebalance` (patches `strimzi.io/rebalance: approve`) and `restart_kafka_connector` (patches `strimzi.io/restart: true`). Uses the shared operator registry for status interpretation.
- **Property-Based Guardrail Tests**: Integrated `hypothesis` and added exhaustive tests for tool argument hashing in `core/tests/test_guardrails.py`, ensuring deterministic and order-invariant hashes for stable confirmation matching.
- **Pub/Sub Worker Health Probes**: The worker now exposes `/healthz` and `/readyz` via the shared `HealthServer`. Readiness flips to 503 if the streaming-pull future dies, so kubelet restarts the pod automatically.

### Changed
- **ADK Upgrade**: Upgraded `google-adk` to **v1.31.0** across the workspace.
- **Experimental Warning Suppression**: pytest is now configured to suppress all experimental feature warnings from `google.adk.features`, keeping test output clean and focused.
- **Helm: Liveness/Readiness + PDB**: `pubsubWorker` deployment now configures liveness, readiness, health port, and an optional `PodDisruptionBudget`.
- **Terraform: DLQ Triage Access**: New `dlq_subscribers` variable grants `roles/pubsub.subscriber` on the DLQ subscription to configured SRE/on-call groups, plus a `dead_letter_subscription_name` output.
- **Docs**: `docs/integrations/google-chat-pubsub.md` now documents every Terraform variable (`chat_publisher_email`, `enable_vertex_ai`, `vertex_ai_project_id`, tuning knobs) and the timeout-alignment rule for Pub/Sub ack deadlines.
- **AEP-018**: Proposal for Pub/Sub idempotency (dedup store on `eventId`) and HPA-on-backlog for the `pubsubWorker` to remove the single-replica SPOF during incidents.

## [0.1.5] - 2026-04-18

### Added
- **Pub/Sub Diagnostics**: Added verbose trace logging for message receipt, parsing, and agent execution to simplify troubleshooting.
- **Heartbeat Monitor**: Implemented a 60-second background heartbeat log in the Pub/Sub worker to provide "proof of life" in container logs.
- **Cross-Project Support**: Explicitly documented and enabled support for subscriptions living in different GCP projects than the agent runner via `GOOGLE_CHAT_PUBSUB_PROJECT`.

### Fixed
- **Cleanup**: Ensured proper cancellation of background heartbeat tasks during worker shutdown.

## [0.1.4] - 2026-04-18

### Added
- **Google Chat Pub/Sub Transport**: Added support for private GKE clusters via Pub/Sub connection type.
- **Terraform Module**: New module in `deploy/terraform/google-chat-bot` for automated GCP infrastructure setup.
- **Helm Expansion**: Added `pubsubWorker` deployment and Workload Identity support to the Helm chart.
- **Overridable Publisher IAM**: Added `chat_publisher_email` variable to handle Domain Restricted Sharing (GCP Org Policy) for Workspace Add-ons.

### Changed
- **Unified Handler**: Refactored Google Chat bot to use a transport-agnostic handler shared between HTTP and Pub/Sub.
- **GKE Deployment Story**: Removed legacy Kustomize manifests in favor of a unified Helm + Terraform production pattern.

### Fixed
- **Poison Message Handling**: Implemented robust `ack`/`nack` logic in the Pub/Sub worker to prevent infinite redelivery of malformed payloads.

## [0.1.3] - 2026-04-13

### Changed
- **Rebranding**: Project renamed from "AI Agents for DevOps" to **Orrery**.
- **Package Renaming**: `ai-agents-core` is now `orrery-core`.
- **Agent Renaming**: `devops-assistant` is now `orrery-assistant`.
- **Infrastructure**: Updated Kubernetes manifests, Helm charts, and Docker images to use the `orrery` namespace and naming.
- **Observability**: Prometheus metrics renamed from `ai_agents_*` to `orrery_*`.

## [0.1.2] - 2026-04-12

### Added

- **Google Chat Async Mode**: Added `GOOGLE_CHAT_ASYNC_RESPONSE` and `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` to `.env.example` for long-running agent support.
- **Enhanced Documentation**: Added Google Chat to the main README and expanded the integration guide with troubleshooting for ADC, scopes, and 401/404/403 errors.

### Changed

- **Google Chat Roadmap**: Moved Google Chat from "Upcoming" to "Current Integrations" in the documentation.
- **API Reference**: Registered `google-chat-bot` for automatic API documentation generation in `mkdocs.yml`.

### Fixed

- **Google Chat Event Parsing**: Implemented robust multi-path parsing for space names, emails, and thread names to prevent 404 errors during asynchronous replies.
- **Async Auth Scope Guidance**: Added detailed troubleshooting and configuration for `403 Forbidden` errors caused by missing `chat.bot` scopes when using Application Default Credentials (ADC).
- **ADC-First Auth Pattern**: Updated Google Chat bot to prioritize Application Default Credentials, enabling seamless Workload Identity support on GKE and Cloud Run.

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
- **Cross-Session Memory**: Enabled `MemoryPlugin` in the `orrery-assistant` agent, allowing it to remember past interactions and save session highlights to the persistent store.
- **Kafka Partition Scaling**: Added `update_kafka_partitions` tool to the Kafka health agent with full unit test coverage.
- **Production deployment hardening** (AEP-011) — complete Kubernetes deployment story
  - Kustomize manifests under `deploy/k8s/` (Deployment, Service, HPA, PDB, NetworkPolicy, ServiceAccount with scoped ClusterRoles)
  - Helm chart under `deploy/helm/orrery-assistant/` with configurable values, NOTES, and `existingSecret` support for out-of-band secret management
  - GHCR CD pipeline (`.github/workflows/docker-publish.yml`) publishing multi-arch (amd64/arm64) images with SBOM and provenance attestation on merges to `main` and `v*.*.*` tags
  - PostgreSQL session store support — `runner.py` honors `DATABASE_URL` (async driver `postgresql+asyncpg://…`) for multi-instance deployments; `core[postgres]` extra adds `asyncpg` and `psycopg2-binary`
  - Rate limiting on the Slack bot `/slack/events` webhook via `slowapi` (configurable via `SLACK_RATE_LIMIT`, default `60/minute`)
  - Root-level `.env.example` documenting every required and optional variable across agents and deployment manifests
  - `docs/deployment.md` — end-to-end production deployment guide (Postgres setup, Helm install, rolling updates, troubleshooting)


### Changed

- `SlackBotConfig.resolve_db_url()` prefers `DATABASE_URL` env var over the legacy `slack_db_url` default — enables sharing Postgres between the Slack bot and the ADK web UI workloads
- `load_agent_env()` and `load_config()` now search for a `.env` file at the project root by default.

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

- **Multi-agent orchestrator** — `orrery-assistant` root agent delegates to 5 specialist agents via `AgentTool` and deterministic sub-agent workflows ([ADR-002](docs/adr/002-agent-tool-vs-sub-agents.md))
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

[Unreleased]: https://github.com/BAHALLA/orrery/compare/v0.1.7...HEAD
[0.1.7]: https://github.com/BAHALLA/orrery/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/BAHALLA/orrery/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/BAHALLA/orrery/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/BAHALLA/orrery/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/BAHALLA/orrery/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/BAHALLA/orrery/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/BAHALLA/orrery/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/BAHALLA/orrery/releases/tag/v0.1.0
