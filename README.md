# 🤖 AI Agents for DevOps & SRE

[![CI](https://github.com/BAHALLA/devops-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/BAHALLA/devops-agents/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue.svg)](https://bahalla.github.io/devops-agents/)
[![License: MIT](https://img.shields.io/github/license/BAHALLA/devops-agents)](https://github.com/BAHALLA/devops-agents/blob/main/LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v1.json)](https://docs.astral.sh/uv/)

An open-source platform for building **autonomous DevOps and SRE agents**. Built with [Google ADK](https://google.github.io/adk-docs/) and managed as a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/).

---

### 🚀 [Read the full documentation at bahalla.github.io/devops-agents](https://bahalla.github.io/devops-agents/)

---

## 💡 Why this project?

DevOps and SRE teams often face a "wall of alerts" and repetitive manual triage. This platform provides **specialist agents** that don't just alert you — they **investigate, correlate, and remediate**.

- **Don't just see a lag spike:** The Kafka agent checks consumer groups, the K8s agent inspects the pods, and the Observability agent queries Prometheus — all in parallel.
- **Don't just restart blindly:** Self-healing loops verify if an action worked and retry with a different strategy if it didn't.
- **Stay in control:** Destructive operations always require human approval via a secure confirmation flow.

## ✨ Key Features

### 🧩 Intelligence & Orchestration
- **Multi-agent coordination** — A root orchestrator delegates to specialists (Kafka, K8s, etc.) via dynamic routing or deterministic pipelines.
- **Self-healing (LoopAgent)** — Closed-loop remediation: **Act** (restart/scale) → **Verify** → **Retry** (up to 3 times).
- **Cross-session memory** — Agents recall past incidents, investigations, and team preferences across sessions.

### 🛡️ Safety & Governance
- **Human-in-the-Loop** — Mutating (`@confirm`) and destructive (`@destructive`) tools require explicit human confirmation.
- **RBAC Hierarchy** — Three-role system (**Viewer**, **Operator**, **Admin**) enforced globally via plugins.
- **Audit Trails** — Every tool call is logged with structured JSON, including user ID and session context.

### 🔌 Integration & Observability
- **Multi-Interface** — Interact via **ADK Web UI**, **CLI**, or **Slack** (with interactive buttons).
- **Observability** — Built-in Prometheus metrics for tool latency, error rates, and circuit breaker states.
- **Context Caching** — Optimized for Gemini models to reduce token usage and latency.

## 🚀 Quick Start (Docker)

The fastest way to try the platform is using Docker Compose.

### Prerequisites
- [Docker](https://docs.docker.com/get-docker/) & Docker Compose
- An API Key (Gemini, Claude, or OpenAI)

### Start the Stack
```bash
# 1. Clone the repo
git clone https://github.com/BAHALLA/devops-agents.git && cd devops-agents

# 2. Start with Gemini (default)
# This launches Kafka (KRaft), PostgreSQL, Prometheus stack, and the Agent.
GOOGLE_API_KEY=your-key docker compose --profile demo up -d

# 3. Open the web UI
open http://localhost:8000
```

*For more setup options (Claude, OpenAI, Local models), see the [Configuration Guide](https://bahalla.github.io/devops-agents/config/general/).*

## 🤖 Available Agents

| Agent | Expertise |
|-------|-----------|
| [**devops-assistant**](https://bahalla.github.io/devops-agents/agents/devops-assistant/) | Root orchestrator, incident triage, and remediation. |
| [**k8s-health**](https://bahalla.github.io/devops-agents/agents/k8s-health/) | Nodes, Pods, Deployments, Logs, Events, and Rollbacks. |
| [**kafka-health**](https://bahalla.github.io/devops-agents/agents/kafka-health/) | Brokers, Topics, Consumer Groups, and Lag monitoring. |
| [**observability**](https://bahalla.github.io/devops-agents/agents/observability/) | Prometheus metrics, Loki logs, and Alertmanager silences. |
| [**docker-agent**](https://bahalla.github.io/devops-agents/agents/docker-agent/) | Container health, stats, logs, and Compose projects. |
| [**slack-bot**](https://bahalla.github.io/devops-agents/agents/slack-bot/) | Interactive Slack integration with confirmation buttons. |
| [**ops-journal**](https://bahalla.github.io/devops-agents/agents/ops-journal/) | Persistent notes and session-level bookmarks. |

## 📚 Documentation

- 🏁 **[Getting Started](https://bahalla.github.io/devops-agents/getting-started/)** — Step-by-step setup and first interaction.
- ⚙️ **[Configuration](https://bahalla.github.io/devops-agents/config/general/)** — LLM providers, env vars, and infrastructure.
- 🛠️ **[Developer Guide](https://bahalla.github.io/devops-agents/adding-an-agent/)** — How to build and test your own specialist agents.
- 🏗️ **[Architecture](https://bahalla.github.io/devops-agents/agent-design-patterns/)** — Design patterns, RBAC, and ADRs.

## ⚖️ License

This project is licensed under the [MIT License](LICENSE).
