# AI Agents

A monorepo of autonomous AI agents built with [Google ADK](https://google.github.io/adk-docs/), managed as a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/).

## Repository Structure

```text
ai-agents/
├── Makefile                    # shortcuts for common commands
├── pyproject.toml              # workspace root
├── docker-compose.yml          # shared infrastructure (Kafka, etc.)
├── core/                       # shared utilities (ai-agents-core)
│   └── ai_agents_core/
│       └── base.py             # create_agent(), load_agent_env()
├── agents/
│   ├── kafka-health/           # Kafka monitoring agent
│   ├── devops-assistant/       # Multi-agent orchestrator
│   └── ops-journal/            # Memory and state patterns
└── docs/                       # per-agent documentation
```

## Agents

| Agent | Type | Description | Docs |
|-------|------|-------------|------|
| **kafka-health-agent** | Single agent | Kafka cluster health, topics, consumer groups, lag | [docs/kafka-health-agent.md](docs/kafka-health-agent.md) |
| **devops-assistant** | Multi-agent | Orchestrator that delegates to kafka + docker sub-agents | [docs/devops-assistant.md](docs/devops-assistant.md) |
| **ops-journal** | Memory/state | Notes, preferences, and session tracking with persistent storage | [docs/ops-journal.md](docs/ops-journal.md) |

## Quick Start

```bash
make install      # install all workspace packages
make infra-up     # start Kafka, Zookeeper, Kafka UI
make run-devops   # launch the devops-assistant in ADK Dev UI
```

Run `make help` to see all available commands.

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

## Prerequisites

- [uv](https://docs.astral.sh/uv/) for Python package management
- [Docker](https://docs.docker.com/get-docker/) for infrastructure and the docker agent
- A Google Cloud Project with Vertex AI API enabled (or an AI Studio API key)

## Adding a New Agent

1. Create a directory under `agents/`:
   ```bash
   mkdir -p agents/my-agent/my_agent
   ```

2. Add a `pyproject.toml` that depends on `ai-agents-core`:
   ```toml
   [project]
   name = "my-agent"
   version = "0.1.0"
   requires-python = ">=3.11"
   dependencies = ["ai-agents-core"]

   [tool.uv.sources]
   ai-agents-core = { workspace = true }
   ```

3. Create `my_agent/__init__.py`:
   ```python
   from . import agent
   ```

4. Create `my_agent/agent.py`:
   ```python
   from ai_agents_core import create_agent, load_agent_env

   load_agent_env(__file__)

   root_agent = create_agent(
       name="my_agent",
       description="What this agent does.",
       instruction="How the agent should behave.",
       tools=[...],
   )
   ```

5. Register in the root `pyproject.toml` and add Makefile targets.

6. Run `make install`.
