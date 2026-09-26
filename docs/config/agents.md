# Agent Configuration

Each agent defines its own configuration class that inherits from `AgentConfig`. Values are loaded from environment variables or the centralized `.env` file at the project root.

## Agent-Specific Settings

| Agent | Variable | Default | Description |
|-------|----------|---------|-------------|
| **kafka-health** | `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka broker address(es) |
| **kafka-health** | `KAFKA_SECURITY_PROTOCOL` | `PLAINTEXT` | `SSL`, `SASL_PLAINTEXT` or `SASL_SSL` for secured clusters; the TLS/SASL settings are listed in the [kafka-health README](https://github.com/BAHALLA/orrery/tree/main/agents/kafka-health#connecting-to-a-secured-cluster-tls--sasl) and validated at startup |
| **k8s-health** | `KUBECONFIG_PATH` | — | Path to kubeconfig file (else `~/.kube/config`, else in-cluster service account) |
| **k8s-health**, **kafka-health** (Strimzi), **elasticsearch** (ECK) | `ORRERY_K8S_CONNECT_TIMEOUT_SECONDS` | `5` | Seconds to connect to the Kubernetes API server |
| same | `ORRERY_K8S_READ_TIMEOUT_SECONDS` | `30` | Seconds to wait for the next bytes of an API response. Bounds every call that does not set its own timeout; a hung call is retried at most once (reads only, never mutations), so it holds a worker thread for about two read timeouts at most. Non-positive or non-finite values fall back to the default |
| **observability** | `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus server URL |
| **observability** | `LOKI_URL` | `http://localhost:3100` | Loki server URL |
| **observability** | `ALERTMANAGER_URL` | `http://localhost:9093` | Alertmanager server URL |
| **elasticsearch** | `ELASTICSEARCH_URL` | `http://localhost:9200` | Elasticsearch REST endpoint |
| **elasticsearch** | `ELASTICSEARCH_USERNAME` | — | Basic auth username |
| **elasticsearch** | `ELASTICSEARCH_PASSWORD` | — | Basic auth password |
| **elasticsearch** | `ELASTICSEARCH_API_KEY` | — | API key authentication |
| **slack-bot** | `SLACK_BOT_TOKEN` | — | Slack bot token (`xoxb-...`) |
| **slack-bot** | `SLACK_APP_TOKEN` | — | App-level token for Socket Mode (`xapp-...`) |
| **slack-bot** | `SLACK_SIGNING_SECRET` | — | Request signing secret |
| **slack-bot** | `SLACK_ADMIN_USERS` | — | Comma-separated Slack user IDs with `admin` role |
| **slack-bot** | `SLACK_OPERATOR_USERS` | — | Comma-separated Slack user IDs with `operator` role |
| **google-chat** | `GOOGLE_CHAT_AUDIENCE` | — | Token audience (must match bot URL) |
| **google-chat** | `GOOGLE_CHAT_ADMIN_EMAILS` | — | Comma-separated emails with `admin` role |
| **google-chat** | `GOOGLE_CHAT_OPERATOR_EMAILS` | — | Comma-separated emails with `operator` role |
| **google-chat** | `GOOGLE_CHAT_ASYNC_RESPONSE` | `true` | Enable async replies via Chat REST API |
| **google-chat** | `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` | — | Optional override for REST API identity |

## Centralized Environment

The platform uses a single `.env` file at the root of the workspace. To configure the agents:

1. Create a `.env` file at the project root: `cp .env.example .env`.
2. Fill in the required global and agent-specific values.

The `load_agent_env()` and `load_config()` helpers in the core library are configured to search for this centralized file automatically.


