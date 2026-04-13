# 🏁 Getting Started

Welcome! This guide will help you set up the AI Agents platform and perform your first system triage in under 5 minutes.

## 📋 Prerequisites

Before you begin, ensure you have the following installed:

*   [Docker](https://docs.docker.com/get-docker/) & Docker Compose
*   [Python 3.11+](https://www.python.org/downloads/) (for local development)
*   [uv](https://docs.astral.sh/uv/) (Fast Python package manager)
*   An LLM API Key (Google Gemini is recommended for the best experience)

---

## 🚀 Quick Start (Docker)

The fastest way to experience the platform is using the pre-configured Docker stack. This launches Kafka (KRaft), PostgreSQL, and the full observability suite alongside the Agent.

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/BAHALLA/devops-agents.git
    cd devops-agents
    ```

2.  **Start the Platform**:
    Replace `your-api-key` with your [Google AI Studio](https://aistudio.google.com/apikey) key.
    ```bash
    GOOGLE_API_KEY=your-api-key docker compose --profile demo up -d
    ```

3.  **Access the Dashboard**:
    Open your browser and navigate to [http://localhost:8000](http://localhost:8000).

!!! success "Success"
    You now have a full autonomous DevOps stack running locally!

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

**The "Magic":** The `devops-assistant` triggers a parallel health check across Kafka, K8s, and Docker. It correlates the data and synthesizes a single, high-level status report.

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
