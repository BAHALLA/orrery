# Getting Started

Welcome! This guide will help you set up Orrery and perform your first system triage in under 5 minutes.

## Prerequisites

To try Orrery you only need:

*   [Docker](https://docs.docker.com/get-docker/)
*   An LLM API key. The examples below use **Google Gemini** because it has a
    free tier and is the quickest to start with, but Orrery is provider-agnostic
    — **Anthropic Claude, OpenAI, and local Ollama models work too** with no code
    changes. See [Using a different LLM provider](#using-a-different-llm-provider).

For local development (modifying agents or the core library) you'll additionally want [Python 3.14+](https://www.python.org/downloads/) and [uv](https://docs.astral.sh/uv/) — see [Local Development Setup](#local-development-setup) below.

---

## Quick Start (Docker — no clone required)

The fastest way to try Orrery is to pull the pre-built image from GHCR — no
clone required.

### Kick the tires (single container, ~30 seconds)

The quickest way to open the web UI and chat with the agent:

!!! tip "Start with the operator console"
    Once it is up, the [web console](integrations/web-console.md) has a
    **Check my environment** button that reports which integrations are actually
    wired — and what to configure when one is not. That is usually the fastest
    way past a first-run failure.

```bash
docker pull ghcr.io/bahalla/orrery:latest

docker run --rm -p 8000:8000 \
  -e GOOGLE_API_KEY=your-api-key \
  ghcr.io/bahalla/orrery:latest
```

Open [http://localhost:8000](http://localhost:8000).

!!! tip "Not using Gemini?"
    Swap the `GOOGLE_API_KEY` line for your provider's variables — e.g.
    `-e MODEL_PROVIDER=anthropic -e MODEL_NAME=anthropic/claude-sonnet-4-20250514 -e ANTHROPIC_API_KEY=sk-ant-...`.
    Full matrix in [Using a different LLM provider](#using-a-different-llm-provider).

!!! info "What you get"
    The UI boots with in-memory session state. Tools that need external systems
    (Kafka, Kubernetes, Prometheus) will report that those systems aren't
    reachable — use the Full stack option below for the complete experience.

!!! warning "Dev mode — no authentication"
    The Docker quick-start and `make run-dev` run **unauthenticated** on
    purpose, so you can try the agent without setting up an IdP. **Do not
    expose this on the public internet.** For any deployment beyond
    localhost, enable JWT auth via `orrery_core.serving.server` — see
    [Production deployment → Step 4](deployment.md#step-4-enable-authentication)
    and the [Security guide](config/security.md).

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
    Override the image tag to pin to a release (e.g. `0.4.1`):
    ```bash
    ORRERY_IMAGE=ghcr.io/bahalla/orrery:0.4.1 \
      docker compose --profile demo up -d
    ```

---

## Using a different LLM provider

Orrery routes every agent through [LiteLLM](https://docs.litellm.ai/), so you can
switch backends with **two environment variables — no code changes**:

| Variable | Purpose |
| --- | --- |
| `MODEL_PROVIDER` | Backend: `gemini` (default), `anthropic`, `openai`, `ollama`, … |
| `MODEL_NAME` | Model identifier (the provider prefix is auto-added if you omit it) |

Set those plus the matching API key for your provider. Whichever way you run
Orrery, it's the same three variables:

=== "Google Gemini (default)"

    ```bash
    MODEL_PROVIDER=gemini
    MODEL_NAME=gemini-2.0-flash
    GOOGLE_API_KEY=your-api-key   # aistudio.google.com/apikey
    ```

=== "Anthropic Claude"

    ```bash
    MODEL_PROVIDER=anthropic
    MODEL_NAME=anthropic/claude-sonnet-4-20250514
    ANTHROPIC_API_KEY=sk-ant-api03-...   # console.anthropic.com
    ```

=== "OpenAI"

    ```bash
    MODEL_PROVIDER=openai
    MODEL_NAME=openai/gpt-4o
    OPENAI_API_KEY=sk-...   # platform.openai.com
    ```

=== "Ollama (local, no key)"

    ```bash
    MODEL_PROVIDER=ollama
    MODEL_NAME=ollama/llama3
    OLLAMA_API_BASE=http://localhost:11434   # ollama pull llama3 first
    ```

**Apply them wherever you launch Orrery:**

*   **Single container** — pass each as `-e`:
    ```bash
    docker run --rm -p 8000:8000 \
      -e MODEL_PROVIDER=anthropic \
      -e MODEL_NAME=anthropic/claude-sonnet-4-20250514 \
      -e ANTHROPIC_API_KEY=sk-ant-api03-... \
      ghcr.io/bahalla/orrery:latest
    ```
*   **Full stack / Compose** — put the same lines in your `.env` (Compose reads it
    automatically) or export them before `docker compose --profile demo up -d`.
*   **Local development** — add them to the root `.env` (see next section).

!!! info "Planner note for non-Gemini backends"
    Planning is off by default (`ORRERY_PLANNER=none`). The `builtin` planner is
    the only Gemini-specific option — it uses Gemini's native thinking tokens and
    falls back to no planner (with a warning) on other providers. For a
    provider-agnostic reasoning trace, set `ORRERY_PLANNER=plan_react`.

For the complete provider matrix, key sourcing, context-caching caveats, and
planner options, see **[General configuration → LLM Provider](config/general.md#llm-provider)**.

---

## Local Development Setup

Follow these steps if you want to modify agents or contribute to the core library.

1.  **Install Dependencies**:
    ```bash
    make install
    ```

2.  **Configure Environment**:
    We use a centralized environment file at the root of the workspace.
    ```bash
    cp .env.example .env
    # Edit .env: set MODEL_PROVIDER / MODEL_NAME and the matching API key.
    # Defaults to Gemini (GOOGLE_API_KEY); see "Using a different LLM provider"
    # above for Anthropic / OpenAI / Ollama.
    ```

3.  **Start Infrastructure**:
    Launch the supporting services (Kafka, Postgres, Prometheus).
    ```bash
    make up
    ```

4.  **Run the Orchestrator**:
    ```bash
    make run-dev
    ```
    The ADK Dev UI will be available at [http://localhost:8000](http://localhost:8000).

!!! warning "Same port as the Docker demo"
    Both `make run-dev` (ADK Dev UI) and `docker compose --profile demo up -d` bind `:8000`. If you're running the Docker demo, `make run-dev` will fail to start — `docker compose down` first, or change one of the ports.

---

## Your First Interaction

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

## Explore Further

*   📋 **[Agents overview](agents-overview.md)** — Every agent, its tools, and what role can call them.
*   ⚙️ **[General configuration](config/general.md)** — Tune LLM providers and infrastructure.
*   🛡️ **[Guardrails & RBAC](guardrails.md)** — Three risk tiers, three roles, and how confirmation works.
*   🔐 **[Security & auth](config/security.md)** — JWT bearer-token verification, claim-to-role mapping, and mounted-secret volumes.
*   🏗️ **[Adding an agent](adding-an-agent.md)** — Build your own specialized DevOps expert.
*   📊 **[Observability](metrics.md)** — Monitor agent performance with Prometheus.
*   🆘 **[Troubleshooting](troubleshooting.md)** — Common errors and their fixes.
