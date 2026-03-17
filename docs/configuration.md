# Configuration

Each agent defines its own config class that inherits from `AgentConfig`. Values are loaded from `.env` files and environment variables.

## Shared settings (all agents)

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | Use Vertex AI (`TRUE`) or AI Studio (`FALSE`) |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project ID (required for Vertex AI) |
| `GOOGLE_CLOUD_LOCATION` | — | GCP region, e.g. `us-central1` (required for Vertex AI) |
| `GOOGLE_API_KEY` | — | API key (required for AI Studio) |
| `GEMINI_MODEL_VERSION` | `gemini-2.0-flash` | Gemini model to use |

## Agent-specific settings

| Agent | Variable | Default | Description |
|-------|----------|---------|-------------|
| kafka-health | `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka broker address(es) |
| k8s-health | `KUBECONFIG_PATH` | — | Path to kubeconfig file |
| observability | `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus server URL |
| observability | `LOKI_URL` | `http://localhost:3100` | Loki server URL |
| observability | `ALERTMANAGER_URL` | `http://localhost:9093` | Alertmanager server URL |
| slack-bot | `SLACK_BOT_TOKEN` | — | Slack bot token (`xoxb-...`) |
| slack-bot | `SLACK_APP_TOKEN` | — | App-level token for Socket Mode (`xapp-...`) |
| slack-bot | `SLACK_SIGNING_SECRET` | — | Request signing secret |
| slack-bot | `SLACK_ADMIN_USERS` | — | Comma-separated Slack user IDs with `admin` role |
| slack-bot | `SLACK_OPERATOR_USERS` | — | Comma-separated Slack user IDs with `operator` role |

## Infrastructure

The included `docker-compose.yml` starts the local infrastructure:

| Service | Port | Description |
|---------|------|-------------|
| Kafka | `9092` | Kafka broker |
| Zookeeper | `2181` | Zookeeper for Kafka |
| Kafka UI | `8080` | Web UI for browsing topics and consumer groups |
| Kafka Exporter | `9308` | Prometheus exporter for Kafka metrics |
| Prometheus | `9090` | Metrics collection and alerting rules |
| Loki | `3100` | Log aggregation |
| Alertmanager | `9093` | Alert routing and silence management |

```bash
make infra-up     # start all services
make infra-down   # stop all services
make infra-reset  # stop and wipe volumes
```

## Docker Compose profiles

| Command | What it starts |
|---------|---------------|
| `docker compose up -d` | Infrastructure only |
| `docker compose --profile demo up -d` | Infrastructure + devops-assistant web UI on `:8000` |
| `docker compose --profile slack up -d` | Infrastructure + Slack bot on `:3000` |
