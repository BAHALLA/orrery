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

### Connecting to a secured cluster (TLS / SASL)

Settings are validated at startup. A cluster that needs SCRAM but was given
PLAIN, or a client certificate without its key, fails at boot with the variable
to fix, not as a broker timeout on the first tool call.

| Variable | Values | Notes |
|---|---|---|
| `KAFKA_SECURITY_PROTOCOL` | `PLAINTEXT` (default), `SSL`, `SASL_PLAINTEXT`, `SASL_SSL` | |
| `KAFKA_SASL_MECHANISM` | `PLAIN`, `SCRAM-SHA-256`, `SCRAM-SHA-512`, `OAUTHBEARER` | required with `SASL_*` |
| `KAFKA_SASL_USERNAME` / `KAFKA_SASL_PASSWORD` | | for PLAIN / SCRAM. Keep the password in a Secret. |
| `KAFKA_SASL_OAUTHBEARER_CLIENT_ID` / `_CLIENT_SECRET` / `_TOKEN_ENDPOINT_URL` / `_SCOPE` | | OIDC client credentials; the token endpoint must be `https://` |
| `KAFKA_SSL_CA_LOCATION` | path | CA bundle for a private CA (the system trust store otherwise) |
| `KAFKA_SSL_CERTIFICATE_LOCATION` / `KAFKA_SSL_KEY_LOCATION` / `KAFKA_SSL_KEY_PASSWORD` | paths | mutual TLS; certificate and key go together |
| `KAFKA_SSL_ENDPOINT_IDENTIFICATION_ALGORITHM` | `https` (default), `none` | `none` skips broker hostname verification, and logs a warning |
| `KAFKA_CLIENT_PROPERTIES` | JSON object | any other librdkafka property, e.g. `{"client.id": "orrery"}`. It cannot override the settings above. |

```bash
# Confluent Cloud / Aiven
KAFKA_BOOTSTRAP_SERVERS=pkc-xxxx.europe-west1.gcp.confluent.cloud:9092
KAFKA_SECURITY_PROTOCOL=SASL_SSL
KAFKA_SASL_MECHANISM=PLAIN
KAFKA_SASL_USERNAME=<api-key>
KAFKA_SASL_PASSWORD=<api-secret>

# Strimzi TLS listener with a KafkaUser (type: tls)
KAFKA_BOOTSTRAP_SERVERS=my-cluster-kafka-bootstrap.kafka:9093
KAFKA_SECURITY_PROTOCOL=SSL
KAFKA_SSL_CA_LOCATION=/etc/kafka/ca/ca.crt                 # <cluster>-cluster-ca-cert
KAFKA_SSL_CERTIFICATE_LOCATION=/etc/kafka/user/user.crt    # the KafkaUser Secret
KAFKA_SSL_KEY_LOCATION=/etc/kafka/user/user.key

# Amazon MSK with SCRAM
KAFKA_SECURITY_PROTOCOL=SASL_SSL
KAFKA_SASL_MECHANISM=SCRAM-SHA-512
```

MSK **IAM** authentication is not supported: it needs a token-signing callback,
not configuration. Use SCRAM or mTLS on MSK.

See the root [README](../../README.md#configuration) for Google AI / Vertex AI config.

## Running

```bash
cd agents/kafka-health
uv run adk web                    # ADK Dev UI
uv run adk run kafka_health_agent # Terminal mode
uv run adk api_server             # API server
```

Or from the repo root — the orchestrator composes this agent alongside every
other specialist, which is how it is meant to be run:

```bash
make run-dev  # ADK Dev UI
make run-cli  # Terminal mode
```
