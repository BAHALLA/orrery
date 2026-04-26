# 🏁 Getting Started

Welcome! This guide will help you set up the AI Agents platform and perform your first system triage in under 5 minutes.

## 📋 Prerequisites

To try Orrery you only need:

*   [Docker](https://docs.docker.com/get-docker/)
*   An LLM API Key (Google Gemini is recommended for the best experience)

For local development (modifying agents or the core library) you'll additionally want [Python 3.14+](https://www.python.org/downloads/) and [uv](https://docs.astral.sh/uv/) — see [Local Development Setup](#local-development-setup) below.

---

## 🚀 Quick Start (Docker — no clone required)

The fastest way to try Orrery is to pull the pre-built image from GHCR — no
clone required.

### Kick the tires (single container, ~30 seconds)

The quickest way to open the web UI and chat with the agent:

```bash
docker pull ghcr.io/bahalla/orrery:latest

docker run --rm -p 8000:8000 \
  -e GOOGLE_API_KEY=your-api-key \
  ghcr.io/bahalla/orrery:latest
```

Open [http://localhost:8000](http://localhost:8000).

!!! info "What you get"
    The UI boots with in-memory session state. Tools that need external systems
    (Kafka, Kubernetes, Prometheus) will report that those systems aren't
    reachable — use the Full stack option below for the complete experience.

### Full stack (Kafka + Postgres + Prometheus + Loki + Alertmanager)

Download the compose file and start everything. Still no clone required:

```bash
curl -O https://raw.githubusercontent.com/BAHALLA/orrery/main/docker-compose.yml

GOOGLE_API_KEY=your-api-key docker compose --profile demo up -d
```

The compose file pulls `ghcr.io/bahalla/orrery:latest` by default.

Open [http://localhost:8000](http://localhost:8000).

!!! success "Success"
    You now have a full autonomous DevOps stack running locally!

!!! tip "Pinning a specific version"
    Override the image tag to pin to a release (e.g. `v0.1.9`):
    ```bash
    ORRERY_IMAGE=ghcr.io/bahalla/orrery:v0.1.9 \
      docker compose --profile demo up -d
    ```

---

## 🛠️ Local Development Setup

Follow these steps if you want to modify agents or contribute to the core library.

1.  **Install Dependencies**:
    ```bash
    make install
    ```

2.  **Configure Environment**:
    We use a centralized environment file at the root of the workspace.
    ```bash
    cp .env.example .env
    # Edit .env and add your GOOGLE_API_KEY
    ```

3.  **Start Infrastructure**:
    Launch the supporting services (Kafka, Postgres, Prometheus).
    ```bash
    make infra-up
    ```

4.  **Run the Orchestrator**:
    ```bash
    make run-devops
    ```
    The ADK Dev UI will be available at [http://localhost:8000](http://localhost:8000).

!!! warning "Same port as the Docker demo"
    Both `make run-devops` (ADK Dev UI) and `docker compose --profile demo up -d` bind `:8000`. If you're running the Docker demo, `make run-devops` will fail to start — `docker compose down` first, or change one of the ports.

---

## 💬 Your First Interaction

Once the platform is running, try these scenarios to see the agents in action:

### 1. Automated System Triage
Ask: **"Is my cluster healthy?"**

**The "Magic":** The `orrery-assistant` triggers a parallel health check across Kafka, K8s, Docker, and Elasticsearch. It correlates the data and synthesizes a single, high-level status report.

### 2. Targeted Investigation
Ask: **"List all pods in the kube-system namespace."**

**The "Magic":** The orchestrator identifies the intent and routes the request directly to the `k8s-health` specialist agent.

### 3. Guarded Operations (Safety)
Ask: **"Scale the 'web-app' deployment to 3 replicas."**

**The "Magic":** The agent identifies this as a mutating operation. It will present an **interactive confirmation** prompt before executing any changes.

---

## 📖 Explore Further

*   📋 **[Agents overview](agents-overview.md)** — Every agent, its tools, and what role can call them.
*   ⚙️ **[General configuration](config/general.md)** — Tune LLM providers and infrastructure.
*   🛡️ **[Guardrails & RBAC](guardrails.md)** — Three risk tiers, three roles, and how confirmation works.
*   🏗️ **[Adding an agent](adding-an-agent.md)** — Build your own specialized DevOps expert.
*   📊 **[Observability](metrics.md)** — Monitor agent performance with Prometheus.
*   🆘 **[Troubleshooting](troubleshooting.md)** — Common errors and their fixes.
