# k8s-health-agent

A single agent with tools for monitoring and managing a Kubernetes cluster.

## Tools

| Tool | Description | Guardrail |
|------|-------------|-----------|
| `get_cluster_info` | Cluster version, platform, and node count | — |
| `get_nodes` | List nodes with status, roles, capacity, and versions | — |
| `list_namespaces` | List all namespaces and their status | — |
| `list_pods` | List pods with status, readiness, and restart counts | — |
| `describe_pod` | Detailed pod info: containers, conditions, resources | — |
| `get_pod_logs` | Tail pod logs with optional container and time filters | — |
| `list_deployments` | List deployments with replica status and images | — |
| `get_deployment_status` | Detailed rollout status and conditions | — |
| `get_events` | Recent events with optional field selector filter | — |
| `scale_deployment` | Scale a deployment to N replicas | `@confirm` |
| `restart_deployment` | Trigger a rolling restart | `@destructive` |

## Diagnosis Flow

The agent follows this investigation pattern:

1. `get_cluster_info` + `get_nodes` — cluster overview
2. `get_events` — recent warnings or errors
3. `describe_pod` + `get_pod_logs` — drill into specific pods
4. `get_deployment_status` — check rollout health

## Environment Variables

Place a `.env` file in `agents/k8s-health/k8s_health_agent/.env`:

```bash
# Optional: path to a specific kubeconfig file
# KUBECONFIG_PATH=/path/to/kubeconfig
```

The agent uses the default kubeconfig (`~/.kube/config`) or in-cluster config automatically.

See the root [README](../../README.md#configuration) for Google AI / Vertex AI config.

## Running

```bash
cd agents/k8s-health
uv run adk web                      # ADK Dev UI
uv run adk run k8s_health_agent     # Terminal mode
```

Or from the repo root:

```bash
make run-k8s      # ADK Dev UI
make run-k8s-cli  # Terminal mode
```
