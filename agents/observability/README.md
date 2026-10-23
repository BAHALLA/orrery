# observability-agent

A single agent with tools for querying Prometheus metrics, searching Loki logs, and managing Alertmanager silences.

## Tools

| Tool | Description | Guardrail |
|------|-------------|-----------|
| `query_prometheus` | Execute an instant PromQL query | — |
| `query_prometheus_range` | Execute a range PromQL query over a time window | — |
| `list_prometheus_metrics` | Discover metric names, optionally filtered | — |
| `get_prometheus_metadata` | Type, help and unit for a metric | — |
| `get_prometheus_rules` | Configured alerting and recording rules with their state | — |
| `get_prometheus_alerts` | List all alerting rules and their states (firing/pending/inactive) | — |
| `get_prometheus_targets` | List all scrape targets and their health (up/down) | — |
| `query_loki_logs` | Run a LogQL query to search logs | — |
| `query_loki_range` | LogQL over a relative window ("the last N hours") | — |
| `get_loki_labels` | List all known label names in Loki | — |
| `get_loki_label_values` | Get all values for a specific label | — |
| `get_active_alerts` | List currently firing alerts from Alertmanager | — |
| `get_alert_groups` | List alerts grouped by their labels | — |
| `get_silences` | List active silences | — |
| `create_silence` | Create a new silence to suppress matching alerts | `@confirm` |
| `delete_silence` | Expire a silence (may cause suppressed alerts to fire) | `@destructive` |

## Environment Variables

Place a `.env` file in `agents/observability/observability_agent/.env`:

```bash
PROMETHEUS_URL=http://localhost:9090     # defaults to http://localhost:9090
LOKI_URL=http://localhost:3100           # defaults to http://localhost:3100
ALERTMANAGER_URL=http://localhost:9093   # defaults to http://localhost:9093
```

See the root [README](../../README.md#configuration) for Google AI / Vertex AI config.

## Running

```bash
cd agents/observability
uv run adk web                        # ADK Dev UI
uv run adk run observability_agent    # Terminal mode
uv run adk api_server                 # API server
```

Or from the repo root — the orchestrator composes this agent alongside every
other specialist, which is how it is meant to be run:

```bash
make run-dev  # ADK Dev UI
make run-cli  # Terminal mode
```

## Local Infrastructure

Start the full observability stack with Docker Compose:

```bash
make up   # Starts Prometheus, Loki, Alertmanager (+ Kafka and the rest of the stack)
```

This provisions demo alert rules (`TargetDown`, `HighPrometheusScrapeLatency`) so you have real firing alerts to test against immediately. Config files live in `infra/`.

| Service | URL |
|---------|-----|
| Prometheus | http://localhost:9090 |
| Loki | http://localhost:3100 |
| Alertmanager | http://localhost:9093 |
