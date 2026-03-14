# devops-assistant

A multi-agent orchestrator that delegates to specialized sub-agents. It has no tools of its own — it routes user requests to the right specialist.

## Agent Graph

```text
devops_assistant (orchestrator)
├── incident_triage_agent (SequentialAgent)
│   ├── health_check_agent (ParallelAgent)
│   │   ├── kafka_health_checker   — Kafka cluster health + lag
│   │   ├── k8s_health_checker     — K8s nodes, events, pods
│   │   └── docker_health_checker  — Container status + stats
│   ├── triage_summarizer          — Synthesizes parallel results
│   └── journal_writer             — Saves report to journal
├── kafka_health_agent             — Ad-hoc Kafka queries
├── k8s_health_agent               — Ad-hoc Kubernetes queries
├── docker_agent                   — Ad-hoc Docker queries
└── ops_journal_agent              — Notes, preferences, session tracking
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

The orchestrator supports two modes of delegation:

### Structured workflows

For broad operations, the orchestrator uses deterministic pipelines built with `SequentialAgent` and `ParallelAgent`:

- *"is everything healthy?"* / *"run a triage"* / *"check all systems"* → `incident_triage_agent`

The incident triage pipeline:
1. **Parallel**: checks Kafka, K8s, and Docker concurrently (each writes to session state via `output_key`)
2. **Sequential**: summarizer reads the parallel results and produces a triage report
3. **Sequential**: journal writer saves the report as a note tagged `incident-triage`

### Ad-hoc delegation

For targeted queries, the LLM reads the sub-agent descriptions and delegates to the right specialist:

- *"what's the consumer lag?"* → `kafka_health_agent`
- *"list all pods in staging"* → `k8s_health_agent`
- *"show me kafka container logs"* → `docker_agent`
- *"save a note about this incident"* → `ops_journal_agent`

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
