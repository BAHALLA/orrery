# elasticsearch-agent

A specialist agent for Elasticsearch cluster operations. Exposes **read-only** tools against the Elasticsearch REST API *and* the Kubernetes control plane (ECK operator CRs).

## Tools

### REST (speak to a live ES cluster)

| Tool | Description | Guardrail |
|------|-------------|-----------|
| `get_cluster_health` | Cluster health (`green`/`yellow`/`red`), node counts, shard counts | — |
| `get_cluster_stats` | Cluster-wide stats: docs, store size, JVM heap | — |
| `get_nodes_info` | Node names, versions, roles, transport addresses | — |
| `get_pending_tasks` | Cluster-level pending tasks (mapping updates, allocations) | — |
| `get_cluster_settings` | Persistent, transient, and default cluster settings | — |
| `list_indices` | Indices matching a pattern, with health and size | — |
| `get_index_stats` | Doc counts, store size, per-shard stats for one index | — |
| `get_index_mappings` | Index mapping (schema) | — |
| `get_index_settings` | Index settings (shards, replicas, ILM policy) | — |
| `get_shard_allocation` | Per-shard state, node assignment, size | — |
| `explain_shard_allocation` | Root cause for an unassigned/stuck shard | — |
| `search` | Query DSL search against an index or index pattern | — |
| `count_documents` | Count documents matching a query (or all) | — |
| `list_index_templates` | Composable index templates | — |
| `list_aliases` | All aliases and their backing indices | — |
| `list_ilm_policies` | ILM policy names and phases | — |
| `explain_ilm_status` | Current ILM phase/step for an index (diagnoses stuck rollovers) | — |
| `list_snapshot_repositories` | Registered snapshot repos (fs/s3/gcs/azure) | — |
| `list_snapshots` | Snapshots in a repository with state and size | — |

### ECK (Kubernetes control plane)

| Tool | Description | Guardrail |
|------|-------------|-----------|
| `list_eck_clusters` | `Elasticsearch` CRs with health, phase, warnings | — |
| `describe_eck_cluster` | Full spec + raw + interpreted status for one cluster | — |
| `list_kibana_instances` | `Kibana` CRs with associated ES ref and health | — |
| `describe_kibana` | Full spec + interpreted status for one Kibana | — |
| `get_eck_operator_events` | Recent warnings/errors from the ECK operator namespace | — |

## Environment variables

Place a `.env` file in `agents/elasticsearch/elasticsearch_agent/.env` (see `.env.example`):

```bash
ELASTICSEARCH_URL=http://localhost:9200
# Authentication — pick one:
# ELASTICSEARCH_API_KEY=base64_encoded_api_key
# ELASTICSEARCH_USERNAME=elastic
# ELASTICSEARCH_PASSWORD=changeme
ELASTICSEARCH_VERIFY_CERTS=true
ELASTICSEARCH_HTTP_TIMEOUT=15
# KUBECONFIG_PATH=   # for ECK tools (defaults to ~/.kube/config, then in-cluster)
```

## Running

```bash
cd agents/elasticsearch
uv run adk web                        # ADK Dev UI
uv run adk run elasticsearch_agent    # Terminal mode
```

Or from the repo root:

```bash
make run-elasticsearch      # ADK Dev UI
make run-elasticsearch-cli  # Terminal mode
```

## Local infrastructure

A gated `elastic` profile ships with the repo's `docker-compose.yml`:

```bash
docker compose --profile elastic up -d elasticsearch kibana
```

| Service | URL |
|---------|-----|
| Elasticsearch | http://localhost:9200 |
| Kibana | http://localhost:5601 |

!!! tip "REST vs ECK tools"
    Use REST tools when you can reach the cluster and need runtime data (shards, docs, search). Use ECK tools when the cluster is down, partially reachable, or when you need to understand *why* the operator is reconciling (phase, events). `describe_eck_cluster` + `get_eck_operator_events` is the fastest path to diagnose a stuck cluster.
