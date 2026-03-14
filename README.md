# AI Agents for DevOps & SRE

An open-source platform for building autonomous DevOps and SRE agents. Built with [Google ADK](https://google.github.io/adk-docs/) and managed as a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/).

Agents can monitor infrastructure, diagnose issues, and take action with built-in safety guardrails that require human confirmation before any destructive operation.

## Key Features

- **Multi-agent orchestration** — a root agent delegates to specialized sub-agents based on user intent
- **Structured workflows** — `SequentialAgent` and `ParallelAgent` for deterministic multi-step pipelines (e.g., incident triage checks Kafka, K8s, Docker in parallel, then summarizes)
- **Safety guardrails** — destructive tools (`@destructive`) require explicit confirmation; mutating tools (`@confirm`) prompt before executing
- **Graceful error handling** — tool and model failures are caught and returned as structured responses so the LLM can reason about them instead of crashing
- **Audit logging** — every tool call is logged with timestamp, agent, arguments, and result
- **Persistent sessions** — SQLite-backed session state, user-scoped notes, and app-wide shared data that survive restarts
- **Composable architecture** — each agent is a standalone package that can run independently or plug into an orchestrator

## Agents

| Agent | Type | Description |
|-------|------|-------------|
| [**core**](core/) | Library | Agent factory, guardrails, error handlers, audit logging, persistent runner, typed config |
| [**kafka-health-agent**](agents/kafka-health/) | Single agent | Kafka cluster health, topics, consumer groups, lag |
| [**k8s-health-agent**](agents/k8s-health/) | Single agent | Kubernetes cluster health, nodes, pods, deployments, logs, events |
| [**devops-assistant**](agents/devops-assistant/) | Multi-agent | Orchestrator that delegates to kafka, k8s, docker, and journal sub-agents |
| [**ops-journal**](agents/ops-journal/) | Memory/state | Notes, preferences, and session tracking with persistent storage |

## Quick Start

### Try it with Docker (no install required)

The only prerequisite is [Docker](https://docs.docker.com/get-docker/) and a [Google AI Studio API key](https://aistudio.google.com/apikey).

```bash
# Clone and start everything — infra + agent web UI
GOOGLE_API_KEY=your-api-key docker compose --profile demo up -d

# Open the web UI
open http://localhost:8000
```

This starts Kafka, Zookeeper, Kafka UI, and the devops-assistant agent with a chat interface.

### Local development

```bash
make install      # install all workspace packages
make infra-up     # start Kafka, Zookeeper, Kafka UI
make run-devops   # launch the devops-assistant in ADK Dev UI
```

Run `make help` to see all available commands.

### Prerequisites

- **Docker only** for the quick start above
- For local development: [uv](https://docs.astral.sh/uv/), [Docker](https://docs.docker.com/get-docker/), and a Google AI Studio API key or Vertex AI project

## Configuration

The platform uses a layered configuration system built on [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/). Each agent defines its own config class that inherits from `AgentConfig`, and values are loaded from `.env` files and environment variables.

### How it works

1. **`AgentConfig`** (in `core`) defines settings shared by all agents (AI provider, model version)
2. Each agent **extends** `AgentConfig` with its own settings (e.g., `KafkaConfig` adds `kafka_bootstrap_servers`)
3. Settings are loaded from the `.env` file next to each agent's module, with environment variables taking precedence

```
agents/kafka-health/kafka_health_agent/.env   ← loaded by kafka-health-agent
agents/k8s-health/k8s_health_agent/.env       ← loaded by k8s-health-agent
agents/devops-assistant/devops_assistant/.env  ← loaded by devops-assistant
agents/ops-journal/ops_journal_agent/.env      ← loaded by ops-journal
```

### Shared settings (all agents)

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | Use Vertex AI (`TRUE`) or AI Studio (`FALSE`) |
| `GOOGLE_CLOUD_PROJECT` | — | GCP project ID (required for Vertex AI) |
| `GOOGLE_CLOUD_LOCATION` | — | GCP region, e.g. `us-central1` (required for Vertex AI) |
| `GOOGLE_API_KEY` | — | API key (required for AI Studio) |
| `GEMINI_MODEL_VERSION` | `gemini-2.0-flash` | Gemini model to use |

### Agent-specific settings

**kafka-health-agent**

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka broker address(es) |

**k8s-health-agent**

| Variable | Default | Description |
|----------|---------|-------------|
| `KUBECONFIG_PATH` | — | Path to kubeconfig file (uses default kubeconfig if unset) |

### Example `.env` file

```bash
# AI provider — choose one:

# Option 1: Vertex AI (recommended)
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=my-gcp-project
GOOGLE_CLOUD_LOCATION=us-central1

# Option 2: Google AI Studio
# GOOGLE_GENAI_USE_VERTEXAI=FALSE
# GOOGLE_API_KEY=your-api-key

# Model
GEMINI_MODEL_VERSION=gemini-2.0-flash

# Agent-specific (kafka-health-agent)
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
```

### Infrastructure

The included `docker-compose.yml` starts the local infrastructure needed by the kafka-health-agent:

| Service | Port | Description |
|---------|------|-------------|
| Kafka | `9092` | Kafka broker |
| Zookeeper | `2181` | Zookeeper for Kafka |
| Kafka UI | `8080` | Web UI for browsing topics and consumer groups |

```bash
make infra-up     # start all services
make infra-down   # stop all services
make infra-reset  # stop and wipe volumes (useful for cluster.id mismatches)
```

### Docker

The platform ships as a single Docker image containing all agents. Docker Compose profiles control which services start.

| Command | What it starts |
|---------|---------------|
| `docker compose up -d` | Infrastructure only (Kafka, Zookeeper, Kafka UI) |
| `docker compose --profile demo up -d` | Infrastructure + devops-assistant web UI on `:8000` |

```bash
# Build the image
make docker-build

# Start the full demo
GOOGLE_API_KEY=your-key make docker-demo

# Stop everything
make docker-down
```

The devops-assistant container mounts the Docker socket (read-only) so it can inspect containers on the host.

## Testing

Tests live next to each package they cover:

```
core/tests/                    # guardrails, error handlers, audit, config
agents/kafka-health/tests/     # Kafka tools
agents/k8s-health/tests/       # Kubernetes tools
agents/devops-assistant/tests/ # Docker tools
agents/ops-journal/tests/      # journal & state tools
```

Run the full suite (132 tests):

```bash
make test
```

Run tests for a single package:

```bash
uv run pytest agents/kafka-health/tests/ -v
```

All external dependencies (Kafka, Kubernetes, Docker) are mocked — no running infrastructure needed.

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
