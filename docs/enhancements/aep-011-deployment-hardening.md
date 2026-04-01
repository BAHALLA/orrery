# AEP-011: Production Deployment Hardening

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P2 |
| **Effort** | High (7-10 days) |
| **Impact** | Critical |
| **Dependencies** | AEP-010 (tracing) |

## Gap Analysis

### Current Implementation
The project has basic deployment support:
- `Dockerfile` and `Dockerfile.prod` for container builds
- `docker-compose.yml` for local stack
- `run_persistent.py` for CLI with SQLite persistence
- `HealthServer` in `core/ai_agents_core/health.py`

Missing for production:
- No Kubernetes manifests or Helm charts
- No readiness/liveness probes integrated with the agent runner
- No graceful shutdown handling
- No horizontal scaling guidance
- No rate limiting
- SQLite doesn't support concurrent access

### What ADK Provides
ADK supports multiple deployment targets:
- **Cloud Run**: Containerized auto-scaling
- **GKE**: Kubernetes-managed deployment
- **Agent Engine (Vertex AI)**: Fully managed service
- **FastAPI entry point**: Standard HTTP server pattern for containers

ADK also provides:
- `adk api_server` for production HTTP serving
- Session service options beyond SQLite (Vertex AI, Firestore)
- The `App` class for wrapping agents with configuration

### Gap
The project is production-ready for single-instance demo deployments but lacks enterprise
deployment patterns for multi-instance, auto-scaling, and zero-downtime deployments.

## Proposed Solution

### Step 1: Health Endpoints

Wire the existing `HealthServer` into the agent runner:

```python
# Health check that verifies agent readiness
async def readiness_check():
    """Returns 200 if the agent is ready to serve requests."""
    checks = {
        "session_service": await check_session_service(),
        "model_available": await check_model_connectivity(),
    }
    all_healthy = all(checks.values())
    return {"status": "ready" if all_healthy else "not_ready", "checks": checks}
```

### Step 2: Graceful Shutdown

Handle SIGTERM for zero-downtime deploys:

```python
import signal
import asyncio

class GracefulShutdown:
    def __init__(self, runner):
        self.runner = runner
        self.shutting_down = False
        signal.signal(signal.SIGTERM, self._handle_sigterm)

    def _handle_sigterm(self, signum, frame):
        self.shutting_down = True
        # Stop accepting new requests
        # Wait for in-flight requests to complete (with timeout)
        asyncio.create_task(self._drain(timeout=30))

    async def _drain(self, timeout):
        # Wait for active sessions to complete
        await asyncio.sleep(timeout)
        sys.exit(0)
```

### Step 3: Kubernetes Manifests

```yaml
# deploy/k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: devops-assistant
spec:
  replicas: 2
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    spec:
      terminationGracePeriodSeconds: 60
      containers:
      - name: devops-assistant
        image: devops-assistant:latest
        ports:
        - containerPort: 8000
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "1Gi"
            cpu: "500m"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 10
        env:
        - name: MODEL_PROVIDER
          value: "gemini"
        - name: MODEL_NAME
          value: "gemini-2.0-flash"
        envFrom:
        - secretRef:
            name: agent-secrets
```

### Step 4: PostgreSQL Session Service

Replace SQLite for multi-instance deployments:

```python
from google.adk.sessions import DatabaseSessionService

# PostgreSQL connection for shared session state
session_service = DatabaseSessionService(
    db_url=os.getenv("DATABASE_URL", "postgresql://user:pass@postgres:5432/agents")
)
```

### Step 5: Rate Limiting

Add rate limiting middleware for the HTTP server:

```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

@app.post("/run")
@limiter.limit("30/minute")
async def run_agent(request: Request):
    ...
```

### Step 6: Helm Chart

```
deploy/
  helm/
    devops-assistant/
      Chart.yaml
      values.yaml
      templates/
        deployment.yaml
        service.yaml
        configmap.yaml
        secret.yaml
        hpa.yaml          # Horizontal Pod Autoscaler
        pdb.yaml          # Pod Disruption Budget
```

## Affected Files

| File | Change |
|------|--------|
| `core/ai_agents_core/health.py` | Add `/ready` endpoint with dependency checks |
| `core/ai_agents_core/runner.py` | Add graceful shutdown handler |
| `deploy/k8s/` | New: Kubernetes manifests |
| `deploy/helm/` | New: Helm chart |
| `docker-compose.prod.yml` | New: production compose with PostgreSQL |
| `core/pyproject.toml` | Add `psycopg2-binary` or `asyncpg` |
| `docs/deployment.md` | New: production deployment guide |

## Acceptance Criteria

- [ ] Health (`/health`) and readiness (`/ready`) endpoints functional
- [ ] Graceful shutdown handles SIGTERM with drain timeout
- [ ] Kubernetes manifests with probes, resource limits, rolling update
- [ ] PostgreSQL session service for multi-instance
- [ ] Rate limiting on HTTP endpoints
- [ ] Helm chart with configurable values
- [ ] HPA (Horizontal Pod Autoscaler) configuration
- [ ] Production deployment documentation
- [ ] Zero-downtime rolling update verified

## Notes

- The ADK `DatabaseSessionService` supports PostgreSQL via SQLAlchemy. Verify that the project's session state schema is compatible.
- For the Slack bot, consider a separate deployment with its own scaling profile (Slack events are bursty).
- Secret management (API keys, database credentials) should use Kubernetes secrets or a vault solution, not environment variables in manifests.
