# devops-assistant

A multi-agent orchestrator that delegates to specialized sub-agents. It has no tools of its own — it routes user requests to the right specialist.

## Agent Graph

```text
devops_assistant (orchestrator)
├── kafka_health_agent   — Kafka cluster operations
├── k8s_health_agent     — Kubernetes cluster operations
├── docker_agent         — Docker container operations
└── ops_journal_agent    — Notes, preferences, and session tracking
```

![DevOps Assistant — agent graph and container inspection](assets/devops-assistant-graph.png)

*The ADK Dev UI showing the agent graph: `devops_assistant` delegates to sub-agents, each with their own tools.*

## Sub-agents

### kafka_health_agent

Reused from the standalone [kafka-health-agent](../kafka-health/). Handles all Kafka cluster operations.

### k8s_health_agent

Reused from the standalone [k8s-health-agent](../k8s-health/). Handles Kubernetes cluster health, nodes, pods, deployments, logs, events, scaling, and restarts. Includes guardrails for destructive operations (scale, restart).

### docker_agent

| Tool | Description |
|------|-------------|
| `list_containers` | List running (or all) Docker containers |
| `inspect_container` | Get detailed info: state, ports, env vars, health |
| `get_container_logs` | Tail recent logs with optional `--since` filter |
| `get_container_stats` | CPU, memory, network, and block I/O stats |
| `docker_compose_status` | Status of services in a Compose project |

### ops_journal_agent

Reused from the standalone [ops-journal](../ops-journal/). Handles notes, preferences, session tracking, and team bookmarks. See that doc for details on state scopes (`session`, `user:`, `app:`, `temp:`).

After a significant investigation, the orchestrator will proactively suggest saving findings as a note via this agent.

## How Delegation Works

The root `devops_assistant` agent has no tools. When a user sends a message, the LLM reads the sub-agent descriptions and decides which specialist to hand off to:

- *"what's the consumer lag?"* → `kafka_health_agent`
- *"list all pods in staging"* → `k8s_health_agent`
- *"show me kafka container logs"* → `docker_agent`
- *"save a note about this incident"* → `ops_journal_agent`
- *"is everything healthy?"* → delegates to multiple agents, then synthesizes

## Running

```bash
cd agents/devops-assistant
uv run adk web                    # ADK Dev UI
uv run adk run devops_assistant   # Terminal mode
```

Or from the repo root:

```bash
make run-devops              # ADK Dev UI (in-memory state)
make run-devops-cli          # Terminal mode (in-memory state)
make run-devops-persistent   # Terminal with SQLite persistence
```

### Persistent Mode

By default (`adk web`), state resets on restart. Use persistent mode to keep `user:*` and `app:*` state across sessions:

```bash
make run-devops-persistent
```

This uses `DatabaseSessionService` with a local SQLite file, so notes and preferences survive restarts. Type `new` to start a fresh session while keeping long-term state.
