# AI Agents for DevOps & SRE

An open-source platform for building autonomous DevOps and SRE agents. Built with [Google ADK](https://google.github.io/adk-docs/) and managed as a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/).

Agents can monitor infrastructure, diagnose issues, and take action with built-in safety guardrails that require human confirmation before any destructive operation.

## Key Features

- **Multi-agent orchestration** — a root agent delegates to specialized sub-agents based on user intent
- **Safety guardrails** — destructive tools (`@destructive`) require explicit confirmation; mutating tools (`@confirm`) prompt before executing
- **Audit logging** — every tool call is logged with timestamp, agent, arguments, and result
- **Persistent memory** — session state, user-scoped notes, and app-wide shared data with SQLite backing
- **Composable architecture** — each agent is a standalone package that can run independently or plug into an orchestrator

## Agents

| Agent | Type | Description |
|-------|------|-------------|
| [**core**](core/) | Library | Agent factory, guardrails, audit logging, typed config |
| [**kafka-health-agent**](agents/kafka-health/) | Single agent | Kafka cluster health, topics, consumer groups, lag |
| [**k8s-health-agent**](agents/k8s-health/) | Single agent | Kubernetes cluster health, nodes, pods, deployments, logs, events |
| [**devops-assistant**](agents/devops-assistant/) | Multi-agent | Orchestrator that delegates to kafka, k8s, docker, and journal sub-agents |
| [**ops-journal**](agents/ops-journal/) | Memory/state | Notes, preferences, and session tracking with persistent storage |

## Quick Start

```bash
make install      # install all workspace packages
make infra-up     # start Kafka, Zookeeper, Kafka UI
make run-devops   # launch the devops-assistant in ADK Dev UI
```

Run `make help` to see all available commands.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) for Python package management
- [Docker](https://docs.docker.com/get-docker/) for infrastructure and the docker agent
- A Google Cloud Project with Vertex AI API enabled (or an AI Studio API key)

## Environment Configuration

Each agent expects a `.env` file in its package directory (e.g., `agents/kafka-health/kafka_health_agent/.env`):

```bash
# Using Vertex AI (Recommended)
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=your-region
GEMINI_MODEL_VERSION=gemini-2.0-flash

# OR using Google AI Studio
GOOGLE_GENAI_USE_VERTEXAI=FALSE
GOOGLE_API_KEY=your-api-key
```

## Testing

Tests live next to each package they cover:

```
core/tests/                    # guardrails, audit, config
agents/kafka-health/tests/     # Kafka tools
agents/k8s-health/tests/       # Kubernetes tools
agents/devops-assistant/tests/ # Docker tools
agents/ops-journal/tests/      # journal & state tools
```

Run the full suite (127 tests):

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
from ai_agents_core import create_agent, load_agent_env, require_confirmation, audit_logger

load_agent_env(__file__)

root_agent = create_agent(
    name="my_agent",
    description="What this agent does.",
    instruction="How the agent should behave.",
    tools=[...],
    before_tool_callback=require_confirmation(),
    after_tool_callback=audit_logger(),
)
```

Register in the root `pyproject.toml` and run `make install`.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on adding new agents, improving existing ones, and submitting pull requests.

## License

This project is licensed under the [MIT License](LICENSE).
