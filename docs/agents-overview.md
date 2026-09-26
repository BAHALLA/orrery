# Agents overview

A quick tour of every agent shipped with the platform — what it does, the tools it exposes, and the role required to invoke the mutating ones.

For how agents are composed inside `orrery-assistant`, see [ADR-003: Graph Workflow Inversion](adr/003-graph-workflow-inversion.md) (which supersedes [ADR-002](adr/002-agent-tool-vs-sub-agents.md)). For how to build your own, see [Adding a new agent](adding-an-agent.md).

## At a glance

| Agent | Package | Tools | Guarded tools | Primary use |
|-------|---------|-------|---------------|-------------|
| [`orrery-assistant`](#orrery-assistant) | `agents/orrery-assistant` | *orchestrator* | delegates | Top-level entry point — routes to specialists and runs triage / remediation workflows |
| [`kafka-health`](#kafka-health) | `agents/kafka-health` | 24 | 9 | Kafka cluster health, topic ops, consumer lag, Strimzi-aware |
| [`k8s-health`](#k8s-health) | `agents/k8s-health` | 26 | 5 | Kubernetes diagnostics, scaling, rollouts, operator-aware (Strimzi / ECK) |
| [`observability`](#observability) | `agents/observability` | 16 | 2 | Prometheus queries, Loki logs, Alertmanager silences |
| [`elasticsearch`](#elasticsearch) | `agents/elasticsearch` | 24 | 0 | ES cluster/index/shard diagnostics, search, ILM, snapshots, ECK-aware |
| [`docker-agent`](#docker-agent) | `agents/docker-agent` | 17 | 6 | Container inspection and lifecycle ops |
| [`ops-journal`](#ops-journal) | `agents/ops-journal` | 10 | 0 | Cross-session notes, preferences, and team bookmarks |

!!! info "Guarded tool counts"
    Counts reflect tools marked `@confirm` (operator+) or `@destructive` (admin-only). Unmarked tools are read-only and accessible to any `viewer`. See [Testing RBAC across surfaces](rbac-testing.md) to exercise each tier.

!!! tip "Restricting *where*, not just *what*"
    Set `ORRERY_PROTECTED_NAMESPACES` (comma-separated globs, e.g. `kube-system,kube-*,monitoring*`) to stop non-admins mutating in infrastructure namespaces. An operator can then restart an application Deployment but not one in `kube-system` — same tool, very different blast radius. Reads are never scoped, so diagnosis still works everywhere. See [RBAC](adr/001-rbac.md).

---

## orrery-assistant

The root orchestrator. The interactive root is `orrery_chat_agent` — a chat-mode ADK 2.0 `LlmAgent` that keeps conversation history and routes each query to the right specialist via `AgentTool`. A separate graph `Workflow` (`orrery_triage_workflow`) provides the deterministic, parallel, bounded-loop incident-response pipeline for batch / scheduled runs (`make run-triage`). Both reuse the same node agents; see [ADR-003](adr/003-graph-workflow-inversion.md).

**Run it:**
```bash
make run-dev  # ADK Dev UI on :8000
make run-cli  # Terminal REPL
make run-cli PERSIST=1 # Persistent sessions + memory (in-memory, or PostgreSQL via DATABASE_URL)
make run-triage              # Deterministic triage Workflow, one batch run
```

**Exposed capabilities:**
- `orrery_chat_agent` (interactive root) — conversational LLM that routes to `kafka_health`, `k8s_health`, `observability`, `elasticsearch`, `docker`, and `ops_journal` via `AgentTool`, plus `incident_triage_agent` for a single-turn full sweep and `LoadMemoryTool` (model-invoked `load_memory`) for cross-session recall
- `orrery_triage_workflow` (batch root) — `health_join` barriers the 5 parallel subsystem checkers, then `triage_summarizer` → `journal_writer` → `triage_route`
- `remediation_actor` / `remediation_verifier` — closed-loop remediation subgraph that acts → verifies → retries up to 3 times via a `verify_route` state counter

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
| `get_topic_config` | viewer | Topic config, split into overridden vs cluster defaults (sensitive values masked) |
| `create_kafka_topic` | operator (`@confirm`) | Create with partitions + replication |
| `update_kafka_partitions` | operator (`@confirm`) | Increase partition count |
| `tune_topic_config` | operator (`@confirm`) | Set a topic setting that cannot delete data (message size, compression, min ISR) |
| `alter_topic_config` | admin (`@destructive`) | Set `retention.*` / `cleanup.policy` — a low retention discards data, so this is gated like a deletion |
| `delete_kafka_topic` | admin (`@destructive`) | Permanent topic deletion |
| `reset_consumer_group_offsets` | admin (`@destructive`) | Reset a group to earliest (reprocess) or latest (drop the backlog); group must be inactive |
| `delete_consumer_group` | admin (`@destructive`) | Delete an inactive group and its committed offsets |

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
| `list_services`, `describe_service` | viewer | Service spec plus ready vs not-ready Endpoints — the usual cause of "connection refused" when the pods look healthy |
| `list_configmaps`, `get_configmap` | viewer | ConfigMap keys and values (long values truncated) |
| `top_nodes`, `top_pods` | viewer | Live CPU/memory usage via the metrics API; degrades cleanly when metrics-server is absent |
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
| `list_prometheus_metrics`, `get_prometheus_metadata` | viewer | Discover metric names and their type/help/unit when the exact name isn't known |
| `get_prometheus_rules` | viewer | Configured alerting + recording rules with their state |
| `query_loki_range` | viewer | LogQL over a relative window ("the last N hours") without computing timestamps |
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
| `list_networks`, `inspect_network` | viewer | Driver, subnets, and which containers are attached |
| `list_volumes`, `inspect_volume` | viewer | Driver, mountpoint, labels |
| `system_df` | viewer | Disk usage by images / containers / volumes / build cache — what to prune, before pruning |
| `start_container`, `restart_container` | operator (`@confirm`) | Lifecycle control |
| `stop_container` | operator (`@confirm`) | Graceful stop with timeout |
| `remove_image` | admin (`@destructive`) | `docker rmi` with force flag |
| `prune_images` | admin (`@destructive`) | Remove unused images (dangling only unless `all_unused`) |
| `prune_containers` | admin (`@destructive`) | Remove all stopped containers |

---

## ops-journal

Demonstrates ADK's four state scopes (session / user / app / temp). Not infrastructure-touching — used for incident notes, team bookmarks, and preferences that persist across sessions.

**Config:** none.

| Tool | Role | Description |
|------|------|-------------|
| `log_operation`, `get_session_summary` | viewer | Session-scoped event log |
| `save_note`, `list_notes`, `search_notes`, `delete_note` | viewer | User-scoped notes (persist across sessions when a `memory_service` is configured) |
| `set_preference`, `get_preferences` | viewer | User-scoped preferences |
| `list_team_bookmarks` | viewer | App-scoped shared bookmarks |
| `add_team_bookmark` | operator | Adds or updates (by name) a bookmark every user sees — `@confirm` |

No external system is touched. Session- and user-scoped tools are unguarded because they only ever affect the caller; `add_team_bookmark` is the exception, because app-scoped state is the one place a single user's write reaches everyone else. Every collection is bounded: the session log keeps its most recent 200 entries (shared with `ActivityPlugin`), and notes (500 per user), preferences (50 per user) and bookmarks (100 per deployment) refuse writes past their limit rather than silently dropping records. Note ids are never reused. See [Cross-session memory](memory.md) for how notes can outlive a single session.

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
| *"Run a full triage and file the report."* | `orrery-assistant` → `incident_triage_agent` (interactive) or `make run-triage` (batch `Workflow`) |
| *"The pod is still unhealthy — try to fix it."* | `orrery-assistant` → remediation subgraph (`remediation_actor` / `verify_route`) |
