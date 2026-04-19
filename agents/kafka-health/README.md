# kafka-health-agent

A single agent with tools for monitoring and managing a Kafka cluster.
Strimzi-aware: understands Kafka resources managed as Kubernetes CRDs (Kafka,
KafkaTopic, KafkaUser, KafkaConnect, etc.) via `orrery_core.default_registry`.

## Core tools — Kafka protocol

| Tool | Description | Guardrail |
|------|-------------|-----------|
| `get_kafka_cluster_health` | Check broker connectivity and cluster status | — |
| `list_kafka_topics` | List all topics | — |
| `get_topic_metadata` | Get partition details, leaders, replicas, and ISRs | — |
| `list_consumer_groups` | List all consumer groups | — |
| `describe_consumer_groups` | Get members, assignments, and state of consumer groups | — |
| `get_consumer_lag` | Calculate per-partition lag for a consumer group | — |
| `create_kafka_topic` | Create a topic with configurable partitions and replication | `@confirm` |
| `update_kafka_partitions` | Increase partition count for a topic | `@confirm` |
| `delete_kafka_topic` | Delete a topic (irreversible — all data lost) | `@destructive` |

## Strimzi tools — Kubernetes control plane

These tools use the Kubernetes API to introspect and manage resources owned by
the Strimzi Kafka operator (`kafka.strimzi.io`).

| Tool | Description | Guardrail |
|------|-------------|-----------|
| `list_strimzi_clusters` | List `Kafka` CRs + interpreted health/phase | — |
| `describe_strimzi_cluster` | Full spec + raw status + interpreted warnings | — |
| `list_strimzi_topics` | List `KafkaTopic` CRs (Topic Operator view) | — |
| `list_kafka_users` | List `KafkaUser` CRs (ACLs/TLS/SCRAM users) | — |
| `get_kafka_rebalance_status` | Cruise Control rebalance progress/proposals | — |
| `approve_kafka_rebalance` | Start a rebalance (annotate CR with `approve`) | `@confirm` |
| `get_kafka_connect_status` | Connector cluster health and task state | — |
| `list_kafka_connectors` | List `KafkaConnector` CRs with failed task count | — |
| `restart_kafka_connector` | Trigger a connector restart via annotation | `@confirm` |
| `get_mirrormaker2_status` | MirrorMaker 2 replication flows and lag | — |

## Environment Variables

Place a `.env` file in `agents/kafka-health/kafka_health_agent/.env`:

```bash
KAFKA_BOOTSTRAP_SERVERS=localhost:9092   # defaults to localhost:9092
# Optional: path to a specific kubeconfig file for Strimzi tools
# KUBECONFIG_PATH=/path/to/kubeconfig
```

See the root [README](../../README.md#configuration) for Google AI / Vertex AI config.

## Running

```bash
cd agents/kafka-health
uv run adk web                    # ADK Dev UI
uv run adk run kafka_health_agent # Terminal mode
uv run adk api_server             # API server
```

Or from the repo root:

```bash
make run-kafka-health      # ADK Dev UI
make run-kafka-health-cli  # Terminal mode
```
