# Agents overview

A quick tour of every agent shipped with the platform — what it does, the tools it exposes, and the role required to invoke the mutating ones.

For how agents are composed inside `devops-assistant`, see [ADR-002: Agent Composition](adr/002-agent-tool-vs-sub-agents.md). For how to build your own, see [Adding a new agent](adding-an-agent.md).

## At a glance

| Agent | Package | Tools | Guarded tools | Primary use |
|-------|---------|-------|---------------|-------------|
| [`devops-assistant`](#devops-assistant) | `agents/devops-assistant` | *orchestrator* | delegates | Top-level entry point — routes to specialists and runs triage / remediation workflows |
| [`kafka-health`](#kafka-health) | `agents/kafka-health` | 9 | 3 | Kafka cluster health, topic ops, consumer lag |
| [`k8s-health`](#k8s-health) | `agents/k8s-health` | 12 | 3 | Kubernetes diagnostics, scaling, rollouts |
| [`observability`](#observability) | `agents/observability` | 12 | 2 | Prometheus queries, Loki logs, Alertmanager silences |
| [`docker-agent`](#docker-agent) | `agents/docker-agent` | 10 | 4 | Container inspection and lifecycle ops |
| [`ops-journal`](#ops-journal) | `agents/ops-journal` | 10 | 0 | Cross-session notes, preferences, and team bookmarks |

!!! info "Guarded tool counts"
    Counts reflect tools marked `@confirm` (operator+) or `@destructive` (admin-only). Unmarked tools are read-only and accessible to any `viewer`. See [Testing RBAC across surfaces](rbac-testing.md) to exercise each tier.

---

## devops-assistant

The root orchestrator. Analyzes user intent and delegates to specialists via either **AgentTool** (LLM-routed) or **SequentialAgent / ParallelAgent / LoopAgent** sub-agents (deterministic pipelines).

**Run it:**
```bash
make run-devops              # ADK Dev UI on :8000
make run-devops-cli          # Terminal REPL
make run-devops-persistent   # SQLite-backed sessions + memory
```

**Exposed capabilities:**
- `incident_triage_agent` — parallel health checks across Kafka, K8s, Docker, and observability, then summarizes and journals
- `kafka_health_agent`, `k8s_health_agent`, `observability_agent`, `docker_agent`, `ops_journal_agent` — direct specialist delegation
- `remediation_pipeline` — `LoopAgent` that acts → verifies → retries up to 3 times; exits when the verifier calls `exit_loop`

---

## kafka-health

Cluster monitoring via `confluent-kafka`'s `AdminClient`. Clients are cached as module-level singletons to avoid per-call reconnect overhead.

**Config:** `KAFKA_BOOTSTRAP_SERVERS=localhost:9092`

| Tool | Role | Description |
|------|------|-------------|
| `get_kafka_cluster_health` | viewer | Broker count, controller ID, reachability |
| `list_kafka_topics` | viewer | All topics, with internal filter |
| `get_topic_metadata` | viewer | Partitions, replicas, ISR |
| `list_consumer_groups` | viewer | Active consumer groups |
| `describe_consumer_groups` | viewer | Member details, state, coordinator |
| `get_consumer_lag` | viewer | Per-partition lag for a group |
| `create_kafka_topic` | operator (`@confirm`) | Create with partitions + replication |
| `update_kafka_partitions` | operator (`@confirm`) | Increase partition count |
| `delete_kafka_topic` | admin (`@destructive`) | Permanent topic deletion |

---

## k8s-health

Kubernetes control-plane tooling via the official Python client. Uses in-cluster config when available, falls back to `~/.kube/config`.

**Config:** `KUBECONFIG_PATH` (optional)

| Tool | Role | Description |
|------|------|-------------|
| `get_cluster_info`, `get_nodes`, `list_namespaces` | viewer | Cluster topology |
| `list_pods`, `describe_pod`, `get_pod_logs` | viewer | Pod diagnostics |
| `list_deployments`, `get_deployment_status` | viewer | Deployment state |
| `get_events` | viewer | Recent Events, optionally filtered by namespace |
| `scale_deployment` | operator (`@confirm`) | Change replica count |
| `restart_deployment` | operator (`@confirm`) | Rolling restart via annotation bump |
| `rollback_deployment` | admin (`@destructive`) | Revert to a previous revision |

---

## observability

A unified interface to Prometheus, Loki, and Alertmanager. HTTP sessions are pooled.

**Config:** `PROMETHEUS_URL`, `LOKI_URL`, `ALERTMANAGER_URL` (all default to their standard local dev ports)

| Tool | Role | Description |
|------|------|-------------|
| `query_prometheus`, `query_prometheus_range` | viewer | Instant + range queries |
| `get_prometheus_alerts`, `get_prometheus_targets` | viewer | Alerting rules and scrape status |
| `query_loki_logs`, `get_loki_labels`, `get_loki_label_values` | viewer | LogQL queries and label discovery |
| `get_active_alerts`, `get_alert_groups`, `get_silences` | viewer | Alertmanager state |
| `create_silence` | operator (`@confirm`) | Silence matcher + duration |
| `delete_silence` | admin (`@destructive`) | Remove an active silence by ID |

---

## docker-agent

Container inspection and lifecycle via the Docker CLI (subprocess). No Docker SDK dependency — works with whatever `docker` binary is on `PATH`.

**Config:** none — inherits the calling environment's Docker context.

| Tool | Role | Description |
|------|------|-------------|
| `list_containers`, `inspect_container` | viewer | State + config (env vars redacted) |
| `get_container_logs`, `get_container_stats` | viewer | Runtime diagnostics |
| `docker_compose_status` | viewer | `docker compose ps` parsing |
| `list_images` | viewer | Local image inventory |
| `start_container`, `restart_container` | operator (`@confirm`) | Lifecycle control |
| `stop_container` | operator (`@confirm`) | Graceful stop with timeout |
| `remove_image` | admin (`@destructive`) | `docker rmi` with force flag |

---

## ops-journal

Demonstrates ADK's four state scopes (session / user / app / temp). Not infrastructure-touching — used for incident notes, team bookmarks, and preferences that persist across sessions.

**Config:** none.

| Tool | Role | Description |
|------|------|-------------|
| `log_operation`, `get_session_summary` | viewer | Session-scoped event log |
| `save_note`, `list_notes`, `search_notes`, `delete_note` | viewer | User-scoped notes (persist across sessions when a `memory_service` is configured) |
| `set_preference`, `get_preferences` | viewer | User-scoped preferences |
| `add_team_bookmark`, `list_team_bookmarks` | viewer | App-scoped shared bookmarks |

All tools are read/write on local session state — no external system is touched, so none are guarded. See [Cross-session memory](memory.md) for how notes can outlive a single session.

---

## Picking an agent

If you're writing a new workflow and need to decide which specialist(s) to call:

| Question | Agent |
|----------|-------|
| *"Is the broker healthy? Who's consuming from topic X? What's the lag?"* | `kafka-health` |
| *"What pods are running? Why is this deployment unhealthy?"* | `k8s-health` |
| *"What are the active alerts? Show me logs matching {job=\"api\"}"* | `observability` |
| *"What containers are up? Restart the web service."* | `docker-agent` |
| *"Remember this incident / recall last week's postmortem."* | `ops-journal` + [memory](memory.md) |
| *"Run a full triage and file the report."* | `devops-assistant` (uses `incident_triage_agent`) |
| *"The pod is still unhealthy — try to fix it."* | `devops-assistant` → `remediation_pipeline` |
