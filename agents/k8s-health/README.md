# k8s-health-agent

A single agent with tools for monitoring and managing a Kubernetes cluster.
Operator-aware: understands workloads managed by **Strimzi** (`kafka.strimzi.io`)
and **ECK** (`*.k8s.elastic.co`) out of the box, and is pluggable for other
operators via `orrery_core.default_registry`.

## Core tools

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
| `rollback_deployment` | Revert to the previous revision | `@destructive` |

## Operator-aware tools

These tools use the operator registry in `orrery_core` to give the agent
visibility into CRs managed by installed operators. Strimzi and ECK are
built-in; others are registered by calling `default_registry.register(...)`.

| Tool | Description |
|------|-------------|
| `detect_operators` | List known operators installed in the cluster (scans CRDs) |
| `list_custom_resources` | List any CR (GVR), enriched with healthy / phase / warnings when the operator is known |
| `describe_custom_resource` | Full CR spec + raw status + an interpreted status block |
| `get_owner_chain` | Walk `ownerReferences` from a Pod up to its root resource |
| `describe_workload` | Pod → operator CR aware: returns the operator's health/phase summary instead of raw pod info |
| `get_operator_events` | Cluster events filtered to operator-watched kinds, optionally narrowed by `operator_name` |

**Example** — a failing Kafka broker pod:

- `describe_pod broker-0 kafka` → shows `CrashLoopBackOff` on the pod.
- `describe_workload broker-0 kafka` → walks `Pod → StatefulSet → Kafka`, reports *"Kafka 'demo' is unhealthy — NotReady: rolling update in progress"*, and surfaces the operator's `.status.conditions` as warnings.

## Diagnosis Flow

The agent follows this investigation pattern:

1. `detect_operators` — learn which operators are installed (run once per session).
2. `get_cluster_info` + `get_nodes` — cluster overview.
3. `get_events` / `get_operator_events` — recent warnings or errors.
4. For operator-managed workloads, prefer `describe_workload` or `describe_custom_resource` over `describe_pod`.
5. `get_pod_logs` — drill into specific pods once the suspect workload is identified.
6. `get_deployment_status` — check rollout health for plain Deployments.

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
