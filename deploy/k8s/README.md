# Kubernetes Manifests

Raw Kustomize manifests for deploying `devops-assistant` to a Kubernetes
cluster. For most users, the Helm chart at `deploy/helm/devops-assistant/`
is the preferred install path — this directory exists for users who prefer
Kustomize or want to review the literal resources that Helm would render.

## Install

```bash
# 1. Create the namespace and service account
kubectl apply -k deploy/k8s/

# 2. Create secrets out-of-band (DO NOT kubectl apply secret.example.yaml)
kubectl -n ai-agents create secret generic devops-assistant-secrets \
  --from-literal=GOOGLE_API_KEY="$GOOGLE_API_KEY" \
  --from-literal=DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/agents"

# 3. Verify
kubectl -n ai-agents get pods
kubectl -n ai-agents logs -l app.kubernetes.io/name=devops-assistant
```

## Resources

| File                  | Purpose                                                          |
|-----------------------|------------------------------------------------------------------|
| `namespace.yaml`      | Creates the `ai-agents` namespace                                |
| `serviceaccount.yaml` | SA + read-only ClusterRole (write role is separate, optional)    |
| `configmap.yaml`      | Non-sensitive config (model, endpoints, feature flags)           |
| `secret.example.yaml` | **Template only** — do not commit real secrets                   |
| `deployment.yaml`     | 2-replica rolling deployment with probes and security context    |
| `service.yaml`        | ClusterIP Service exposing HTTP + /metrics                       |
| `hpa.yaml`            | HPA scaling 2-6 replicas on CPU/memory                           |
| `pdb.yaml`            | PodDisruptionBudget guaranteeing 1 pod available                 |
| `networkpolicy.yaml`  | Egress restriction to in-cluster + HTTPS to public LLM providers |

## Zero-downtime rolling updates

The deployment uses `maxSurge: 1, maxUnavailable: 0` so a new replica must
become ready (via `readinessProbe` on `/readyz`) before an old one is
retired. Combined with the 10-second `preStop` sleep and 60-second
`terminationGracePeriodSeconds`, this provides graceful draining of
in-flight LLM calls.

Verify:

```bash
# Trigger rollout
kubectl -n ai-agents set image deployment/devops-assistant \
  devops-assistant=ghcr.io/bahalla/devops-agents:v0.2.0

# Watch pods cycle with no downtime
kubectl -n ai-agents rollout status deployment/devops-assistant

# Rollback if needed
kubectl -n ai-agents rollout undo deployment/devops-assistant
```
