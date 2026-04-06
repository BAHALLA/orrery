# AI Agents for DevOps & SRE

[![CI](https://github.com/BAHALLA/devops-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/BAHALLA/devops-agents/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue.svg)](https://bahalla.github.io/devops-agents/)
[![License: MIT](https://img.shields.io/github/license/BAHALLA/devops-agents)](https://github.com/BAHALLA/devops-agents/blob/main/LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v1.json)](https://docs.astral.sh/uv/)

An open-source platform for building autonomous DevOps and SRE agents. Built with [Google ADK](https://google.github.io/adk-docs/) and managed as a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/).

---

### 🚀 [Read the full documentation at bahalla.github.io/devops-agents](https://bahalla.github.io/devops-agents/)

---

## What is this?

This repository contains a collection of specialist agents that can monitor infrastructure (Kafka, K8s, Prometheus), diagnose issues, and take action — with built-in safety guardrails that require human confirmation before any destructive operation.

Interact via the **ADK web UI**, **terminal**, or directly from **Slack**.

![Slack Bot Demo](docs/images/slack-bot-demo.png)

## Key Features

- **Multi-agent orchestration** — Root agent delegates to specialists via `AgentTool` or deterministic sub-agent workflows.
- **Slack integration** — Chat with agents from Slack, featuring interactive Approve/Deny buttons for guarded operations.
- **Safety first** — Destructive tools (`@destructive`) and mutating tools (`@confirm`) require explicit human confirmation.
- **Observability** — Every tool call is instrumented with Prometheus metrics (latency, errors, token tracking).
- **Extensible** — Add new agents using the [Agent Factory](https://bahalla.github.io/devops-agents/core/README.md) and standardized plugins.

## Quick Start (Docker)

The fastest way to try the platform is using Docker Compose. You only need an API key from a supported provider (Gemini, Claude, or OpenAI).

```bash
# Clone the repo
git clone https://github.com/BAHALLA/devops-agents.git && cd devops-agents

# Start the full stack with Gemini (default)
GOOGLE_API_KEY=your-key docker compose --profile demo up -d

# Open the web UI
open http://localhost:8000
```

## Available Agents

| Agent | Description |
|-------|-------------|
| [**devops-assistant**](https://bahalla.github.io/devops-agents/agents/devops-assistant/) | Orchestrator for specialist agents and incident triage. |
| [**kafka-health**](https://bahalla.github.io/devops-agents/agents/kafka-health/) | Monitor brokers, topics, and consumer lag. |
| [**k8s-health**](https://bahalla.github.io/devops-agents/agents/k8s-health/) | Inspect nodes, pods, deployments, and logs. |
| [**observability**](https://bahalla.github.io/devops-agents/agents/observability/) | Query Prometheus metrics and Loki logs. |
| [**slack-bot**](https://bahalla.github.io/devops-agents/agents/slack-bot/) | Interactive Slack integration for all agents. |
| [**ops-journal**](https://bahalla.github.io/devops-agents/agents/ops-journal/) | Persistent memory for notes and session state. |

## Documentation Sections

- 📖 **[Getting Started](https://bahalla.github.io/devops-agents/configuration/)** — Setup, environment variables, and Docker profiles.
- 💬 **[Slack Setup](https://bahalla.github.io/devops-agents/slack-setup/)** — App manifest and bot configuration.
- 📊 **[Metrics Guide](https://bahalla.github.io/devops-agents/metrics/)** — Monitoring tool calls with Prometheus/Grafana.
- 🛠️ **[Adding an Agent](https://bahalla.github.io/devops-agents/adding-an-agent/)** — Step-by-step guide for developers.
- 📜 **[Architecture (ADRs)](https://bahalla.github.io/devops-agents/adr/001-rbac/)** — Design decisions for RBAC and composition.

## License

This project is licensed under the [MIT License](LICENSE).
