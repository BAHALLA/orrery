# 🏁 Getting Started

This guide will help you set up the AI Agents platform and perform your first interaction.

## 📋 Prerequisites

Before you begin, ensure you have the following installed:
- [Docker](https://docs.docker.com/get-docker/) & Docker Compose
- [Python 3.11+](https://www.python.org/downloads/) (for local development)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- An API Key for one of the following:
    - **Google Gemini** (Recommended): [Get a key from AI Studio](https://aistudio.google.com/apikey)
    - **Anthropic Claude**: [Get a key from Anthropic Console](https://console.anthropic.com/)
    - **OpenAI GPT-4**: [Get a key from OpenAI Platform](https://platform.openai.com/)

---

## 🚀 Quick Start (Docker)

The fastest way to get running is using the pre-configured Docker stack.

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/BAHALLA/devops-agents.git
    cd devops-agents
    ```

2.  **Start the Services**:
    Replace `your-api-key` with your actual Google AI Studio key.
    ```bash
    GOOGLE_API_KEY=your-api-key docker compose --profile demo up -d
    ```

3.  **Access the Web UI**:
    Open your browser and navigate to `http://localhost:8000`.

---

## 🛠️ Local Development Setup

If you want to modify agents or build your own, follow these steps:

1.  **Install Dependencies**:
    ```bash
    make install
    ```

2.  **Configure Environment**:
    Create a centralized `.env` file at the project root:
    ```bash
    cp .env.example .env
    # Edit .env and add your API keys
    ```

3.  **Start the Infrastructure**:
    This launches Kafka (KRaft), PostgreSQL, Prometheus, and other diagnostic tools.
    ```bash
    make infra-up
    ```

4.  **Run the Agent**:
    ```bash
    make run-devops
    ```
    The agent will be available at `http://localhost:8000`.

---

## 💬 Your First Interaction

Once the platform is running, try these scenarios to see the agents in action:

### 1. System Triage
Ask: *"Is my cluster healthy?"*
**What happens:** The `devops-assistant` triggers a parallel health check across Kafka, K8s, and Docker. It then synthesizes the results into a single report.

### 2. Ad-hoc Query
Ask: *"List all pods in the kube-system namespace."*
**What happens:** The orchestrator routes the request directly to the `k8s-health` specialist agent.

### 3. Guarded Operation
Ask: *"Scale the 'web-app' deployment to 3 replicas."*
**What happens:** The agent identifies this as a mutating operation. It will ask for your **explicit confirmation** before proceeding.

---

## 📖 Next Steps

- **[Configuration Guide](config/general.md)** — Deep dive into LLM providers and environment variables.
- **[Slack Integration](integrations/slack.md)** — Bring your agents into your team's Slack channels.
- **[Adding an Agent](adding-an-agent.md)** — Learn how to build your own specialist agents using our core library.
- **[Metrics & Observability](metrics.md)** — Monitor your agents using the built-in Prometheus dashboard.
