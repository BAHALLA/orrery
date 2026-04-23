# Agents overview

A quick tour of every agent shipped with the platform — what it does, the tools it exposes, and the role required to invoke the mutating ones.

For how agents are composed inside `orrery-assistant`, see [ADR-002: Agent Composition](adr/002-agent-tool-vs-sub-agents.md). For how to build your own, see [Adding a new agent](adding-an-agent.md).

## At a glance

| Agent | Package | Tools | Guarded tools | Primary use |
|-------|---------|-------|---------------|-------------|
| [`orrery-assistant`](#orrery-assistant) | `agents/orrery-assistant` | *orchestrator* | delegates | Top-level entry point — routes to specialists and runs triage / remediation workflows |
| [`kafka-health`](#kafka-health) | `agents/kafka-health` | 19 | 5 | Kafka cluster health, topic ops, consumer lag, Strimzi-aware |
| [`k8s-health`](#k8s-health) | `agents/k8s-health` | 18 | 3 | Kubernetes diagnostics, scaling, rollouts, operator-aware (Strimzi / ECK) |
| [`observability`](#observability) | `agents/observability` | 12 | 2 | Prometheus queries, Loki logs, Alertmanager silences |
| [`elasticsearch`](#elasticsearch) | `agents/elasticsearch` | 24 | 0 | ES cluster/index/shard diagnostics, search, ILM, snapshots, ECK-aware |
| [`docker-agent`](#docker-agent) | `agents/docker-agent` | 10 | 4 | Container inspection and lifecycle ops |
| [`ops-journal`](#ops-journal) | `agents/ops-journal` | 10 | 0 | Cross-session notes, preferences, and team bookmarks |

!!! info "Guarded tool counts"
    Counts reflect tools marked `@confirm` (operator+) or `@destructive` (admin-only). Unmarked tools are read-only and accessible to any `viewer`. See [Testing RBAC across surfaces](rbac-testing.md) to exercise each tier.

---

## orrery-assistant

The root orchestrator. Analyzes user intent and delegates to specialists via either **AgentTool** (LLM-routed) or **SequentialAgent / ParallelAgent / LoopAgent** sub-agents (deterministic pipelines).

**Run it:**
```bash
make run-devops              # ADK Dev UI on :8000
make run-devops-cli          # Terminal REPL
make run-devops-persistent   # SQLite-backed sessions + memory
```

**Exposed capabilities:**
- `incident_triage_agent` — parallel health checks across Kafka, K8s, Docker, observability, and Elasticsearch, then summarizes and journals
- `kafka_health_agent`, `k8s_health_agent`, `observability_agent`, `elasticsearch_agent`, `docker_agent`, `ops_journal_agent` — direct specialist delegation
- `remediation_pipeline` — `LoopAgent` that acts → verifies → retries up to 3 times; exits when the verifier calls `exit_loop`

---

## kafka-health

Cluster monitoring via `confluent-kafka`'s `AdminClient`. Clients are cached as module-level singletons to avoid per-call reconnect overhead.

**Config:** `KAFKA_BOOTSTRAP_SERVERS=localhost:9092`, `KUBECONFIG_PATH` (optional, for Strimzi tools)

**Core tools — Kafka protocol:**

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

**Strimzi tools — Kubernetes control plane** (backed by `orrery_core.default_registry.StrimziDetector`):

| Tool | Role | Description |
|------|------|-------------|
| `list_strimzi_clusters` | viewer | List `Kafka` CRs, enriched with healthy / phase / warnings |
| `describe_strimzi_cluster` | viewer | Full spec + raw status + interpreted status for a `Kafka` CR |
| `list_strimzi_topics` | viewer | List `KafkaTopic` CRs (Topic Operator view), optional `strimzi.io/cluster` label filter |
| `list_kafka_users` | viewer | List `KafkaUser` CRs with authentication / authorization type |
| `get_kafka_connect_status` | viewer | Describe a `KafkaConnect` cluster — REST URL, replicas, loaded plugins |
| `list_kafka_connectors` | viewer | List `KafkaConnector` CRs with task state + failed-task count |
| `get_mirrormaker2_status` | viewer | Describe a `KafkaMirrorMaker2` — clusters, replication flows, per-connector state |
| `get_kafka_rebalance_status` | viewer | Cruise Control `KafkaRebalance` state + optimization result |
| `approve_kafka_rebalance` | operator (`@confirm`) | Annotate `strimzi.io/rebalance: approve` — only when state is `ProposalReady` |
| `restart_kafka_connector` | operator (`@confirm`) | Annotate `strimzi.io/restart: true` on a connector CR |

!!! tip "Kafka protocol vs. Strimzi CRs"
    `list_kafka_topics` returns what the broker actually serves; `list_strimzi_topics` returns what the Topic Operator *wants* the broker to serve. Divergence between the two usually means the operator is reconciling or the TO has errored — check `describe_custom_resource` on the offending `KafkaTopic` in `k8s-health`.

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

**Operator-aware tools** — backed by `orrery_core.default_registry` (Strimzi + ECK built in; pluggable for others):

| Tool | Role | Description |
|------|------|-------------|
| `detect_operators` | viewer | Scan CRDs and report which known operators are installed |
| `list_custom_resources` | viewer | List any CR (GVR), enriched with healthy / phase / warnings when the group is known |
| `describe_custom_resource` | viewer | Full CR spec + raw status + interpreted status block |
| `get_owner_chain` | viewer | Walk `ownerReferences` from a Pod up to its root resource |
| `describe_workload` | viewer | Pod → operator-managed CR aware: returns the operator's health/phase summary instead of raw pod info |
| `get_operator_events` | viewer | Cluster events filtered to operator-watched kinds (e.g., only `Kafka` / `Elasticsearch` events) |

!!! tip "Why this matters"
    For a failing `kafka-broker-0` pod, `describe_pod` shows a `CrashLoopBackOff`; `describe_workload` walks to the owning `Kafka` CR and reports *"Kafka 'demo' is unhealthy — NotReady: rolling update in progress"*. The operator's view is usually the one you want.

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

## elasticsearch

Elasticsearch cluster operations, split across two surfaces: **REST tools** that speak the Elasticsearch wire protocol against a live cluster, and **ECK tools** that introspect the Kubernetes control plane (`*.k8s.elastic.co` CRs) via the operator registry. HTTP sessions and the Kubernetes client are pooled.

**Config:** `ELASTICSEARCH_URL` (default `http://localhost:9200`), `ELASTICSEARCH_API_KEY` *or* `ELASTICSEARCH_USERNAME` + `ELASTICSEARCH_PASSWORD`, `ELASTICSEARCH_VERIFY_CERTS`, `ELASTICSEARCH_CA_CERTS`, `ELASTICSEARCH_HTTP_TIMEOUT`, `KUBECONFIG_PATH` (optional, for ECK tools)

**REST tools — Elasticsearch wire protocol:**

| Tool | Role | Description |
|------|------|-------------|
| `get_cluster_health` | viewer | green/yellow/red status + shard counters |
| `get_cluster_stats` | viewer | Node roles, OS/JVM summary, indices totals |
| `get_nodes_info` | viewer | Per-node roles, version, host details |
| `get_pending_tasks` | viewer | Master-queue backlog — non-empty means the cluster is behind |
| `get_cluster_settings` | viewer | Persistent + transient cluster-level settings |
| `list_indices` | viewer | `_cat/indices` with optional wildcard filter |
| `get_index_stats` | viewer | Docs, storage, segment counts for an index |
| `get_index_mappings` | viewer | Field mapping document |
| `get_index_settings` | viewer | Index-level settings (refresh interval, replicas, etc.) |
| `get_shard_allocation` | viewer | `_cat/shards` — states, sizes, assigned nodes |
| `explain_shard_allocation` | viewer | `_cluster/allocation/explain` — why a shard is unassigned |
| `search` | viewer | Full `_search` with request body + size/from |
| `count_documents` | viewer | `_count` with optional query body |
| `list_index_templates` | viewer | Composable index templates |
| `list_aliases` | viewer | Alias → index mapping |
| `list_ilm_policies` | viewer | Configured ILM policies |
| `explain_ilm_status` | viewer | ILM state per index — the usual place "why is rollover stuck" is answered |
| `list_snapshot_repositories` | viewer | Registered snapshot repos |
| `list_snapshots` | viewer | Snapshots in a repo + state/duration |

**ECK tools — Kubernetes control plane** (backed by `orrery_core.default_registry.ECKDetector`):

| Tool | Role | Description |
|------|------|-------------|
| `list_eck_clusters` | viewer | List `Elasticsearch` CRs, enriched with healthy / phase / warnings |
| `describe_eck_cluster` | viewer | Full spec + nodeSets + raw status + interpreted status for an `Elasticsearch` CR |
| `list_kibana_instances` | viewer | List `Kibana` CRs with version, count, associated ES ref |
| `describe_kibana` | viewer | Full spec + raw status + interpreted status for a `Kibana` CR |
| `get_eck_operator_events` | viewer | Recent Kubernetes Events from the operator namespace (default `elastic-system`) |

!!! tip "Wire protocol vs. ECK CRs"
    `get_cluster_health` reflects what the Elasticsearch data plane is *actually* doing; `list_eck_clusters` reflects what the operator is *trying* to do. If the REST health is green but the ECK CR is stuck in `ApplyingChanges`, the operator is mid-reconcile — check `get_eck_operator_events` for stalls.

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
| *"Is the Strimzi Kafka / ECK Elasticsearch cluster healthy? Why is broker-0 failing?"* | `k8s-health` (via `describe_workload`, `describe_custom_resource`) |
| *"What are the active alerts? Show me logs matching {job=\"api\"}"* | `observability` |
| *"Is the ES cluster green? Why is this shard unassigned? Is ILM stuck?"* | `elasticsearch` |
| *"Is the ECK `Elasticsearch` / `Kibana` CR reconciled? What's the operator doing?"* | `elasticsearch` (via `list_eck_clusters`, `get_eck_operator_events`) |
| *"What containers are up? Restart the web service."* | `docker-agent` |
| *"Remember this incident / recall last week's postmortem."* | `ops-journal` + [memory](memory.md) |
| *"Run a full triage and file the report."* | `orrery-assistant` (uses `incident_triage_agent`) |
| *"The pod is still unhealthy — try to fix it."* | `orrery-assistant` → `remediation_pipeline` |
