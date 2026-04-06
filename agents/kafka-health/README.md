# kafka-health-agent

A single agent with tools for monitoring and managing a Kafka cluster.

## Tools

| Tool | Description |
|------|-------------|
| `get_kafka_cluster_health` | Check broker connectivity and cluster status |
| `list_kafka_topics` | List all topics |
| `create_kafka_topic` | Create a topic with configurable partitions and replication |
| `delete_kafka_topic` | Delete a topic |
| `get_topic_metadata` | Get partition details, leaders, replicas, and ISRs |
| `list_consumer_groups` | List all consumer groups |
| `describe_consumer_groups` | Get members, assignments, and state of consumer groups |
| `get_consumer_lag` | Calculate per-partition lag for a consumer group |

## Environment Variables

Place a `.env` file in `agents/kafka-health/kafka_health_agent/.env`:

```bash
KAFKA_BOOTSTRAP_SERVERS=localhost:9092   # defaults to localhost:9092
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
