# AI Agents for DevOps & SRE

An open-source platform for building autonomous DevOps and SRE agents. Built with [Google ADK](https://google.github.io/adk-docs/) and managed as a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/).

Agents can monitor infrastructure, diagnose issues, and take action — with built-in safety guardrails that require human confirmation before any destructive operation. Interact via the ADK web UI, terminal, or directly from Slack.

![Slack Bot Demo](docs/images/slack-bot-demo.png)

## Key Features

- **Multi-agent orchestration** — a root agent delegates to specialized sub-agents based on user intent
- **Structured workflows** — `SequentialAgent` and `ParallelAgent` for deterministic multi-step pipelines (e.g., incident triage checks Kafka, K8s, Docker, and observability in parallel, then summarizes)
- **Slack integration** — chat with the agent from Slack, with interactive Approve/Deny buttons for guarded operations
- **Safety guardrails** — destructive tools (`@destructive`) require explicit confirmation; mutating tools (`@confirm`) prompt before executing
- **Structured logging** — JSON-formatted logs to stdout, ready for Loki/ELK/Cloud Logging; every tool call is audited with timestamp, agent, arguments, and result
- **Persistent sessions** — SQLite-backed session state, user-scoped notes, and app-wide shared data that survive restarts
- **Composable architecture** — each agent is a standalone package that can run independently or plug into an orchestrator

## Agents

| Agent | Type | Description |
|-------|------|-------------|
| [**core**](core/) | Library | Agent factory, guardrails, error handlers, structured logging, audit trail, activity tracking, persistent runner, typed config |
| [**kafka-health-agent**](agents/kafka-health/) | Single agent | Kafka cluster health, topics, consumer groups, lag |
| [**k8s-health-agent**](agents/k8s-health/) | Single agent | Kubernetes cluster health, nodes, pods, deployments, logs, events |
| [**observability-agent**](agents/observability/) | Single agent | Prometheus metrics/alerts, Loki log queries, Alertmanager silence management |
| [**devops-assistant**](agents/devops-assistant/) | Multi-agent | Orchestrator that delegates to kafka, k8s, observability, docker, and journal sub-agents |
| [**ops-journal**](agents/ops-journal/) | Memory/state | Notes, preferences, and session tracking with persistent storage |
| [**slack-bot**](agents/slack-bot/) | Integration | Slack bot with thread-based sessions and interactive confirmation buttons |

## Quick Start

### Try it with Docker (no install required)

The only prerequisite is [Docker](https://docs.docker.com/get-docker/) and a [Google AI Studio API key](https://aistudio.google.com/apikey).

```bash
# Clone and start everything — infra + agent web UI
GOOGLE_API_KEY=your-api-key docker compose --profile demo up -d

# Open the web UI
open http://localhost:8000
```

This starts Kafka, Zookeeper, Kafka UI, Prometheus, Loki, Alertmanager, and the devops-assistant agent with a chat interface.

### Local development

```bash
make install      # install all workspace packages
make infra-up     # start Kafka, Zookeeper, Prometheus, Loki, Alertmanager
make run-devops   # launch the devops-assistant in ADK Dev UI
```

Run `make help` to see all available commands.

### Prerequisites

- **Docker only** for the quick start above
- For local development: [uv](https://docs.astral.sh/uv/), [Docker](https://docs.docker.com/get-docker/), and a Google AI Studio API key or Vertex AI project

## Slack Bot

The platform includes a Slack bot that lets you interact with the DevOps agent directly from Slack. Each thread becomes a separate conversation, and guarded tools post interactive Approve/Deny buttons.

### Setup

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From manifest** and paste:

```json
{
  "display_information": { "name": "DevOps Agent" },
  "features": { "bot_user": { "display_name": "DevOps Agent", "always_online": true } },
  "oauth_config": {
    "scopes": {
      "bot": ["chat:write", "channels:history", "groups:history", "im:history", "app_mentions:read"]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": ["message.channels", "message.groups", "message.im", "app_mention"]
    },
    "interactivity": { "is_enabled": true },
    "socket_mode_enabled": true,
    "token_rotation_enabled": false
  }
}
```

2. Generate an **App-Level Token** (Basic Information → App-Level Tokens, scope: `connections:write`)
3. **Install to Workspace** and copy the Bot Token
4. Configure `agents/slack-bot/.env`:

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=your-signing-secret
SLACK_APP_TOKEN=xapp-your-app-token
GOOGLE_API_KEY=your-google-api-key
```

5. Run:

```bash
make infra-up              # start infrastructure
make run-slack-bot-socket  # start the bot (Socket Mode, no public URL needed)
```

6. Invite the bot to a channel (`/invite @DevOps Agent`) and start chatting.

See the full [Slack Bot README](agents/slack-bot/README.md) for webhook mode, Docker deployment, and configuration reference.

## Configuration

Each agent defines its own config class that inherits from `AgentConfig`. Values are loaded from `.env` files and environment variables.

### Shared settings (all agents)

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | Use Vertex AI (`TRUE`) or AI Studio (`FALSE`) |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project ID (required for Vertex AI) |
| `GOOGLE_CLOUD_LOCATION` | — | GCP region, e.g. `us-central1` (required for Vertex AI) |
| `GOOGLE_API_KEY` | — | API key (required for AI Studio) |
| `GEMINI_MODEL_VERSION` | `gemini-2.0-flash` | Gemini model to use |

### Agent-specific settings

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

### Infrastructure

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

### Docker

| Command | What it starts |
|---------|---------------|
| `docker compose up -d` | Infrastructure only |
| `docker compose --profile demo up -d` | Infrastructure + devops-assistant web UI on `:8000` |
| `docker compose --profile slack up -d` | Infrastructure + Slack bot on `:3000` |

## Testing

Run the full suite (211 tests):

```bash
make test
```

Run tests for a single package:

```bash
uv run pytest agents/kafka-health/tests/ -v
```

All external dependencies (Kafka, Kubernetes, Docker, Slack) are mocked — no running infrastructure needed.

## Adding a New Agent

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide. The short version:

```bash
mkdir -p agents/my-agent/my_agent
```

```python
# my_agent/agent.py
from ai_agents_core import (
    audit_logger,
    create_agent,
    graceful_tool_error,
    load_agent_env,
    require_confirmation,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="my_agent",
    description="What this agent does.",
    instruction="How the agent should behave.",
    tools=[...],
    before_tool_callback=require_confirmation(),
    after_tool_callback=audit_logger(),
    on_tool_error_callback=graceful_tool_error(),
)
```

Register in the root `pyproject.toml` and run `make install`.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on adding new agents, improving existing ones, and submitting pull requests.

## License

This project is licensed under the [MIT License](LICENSE).
