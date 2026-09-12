# Orrery

**SRE agents you can let near production.**

Autonomous DevOps & SRE agents that investigate, correlate and remediate — with a
human gate on every destructive action. Specialists for Kafka, Kubernetes,
Elasticsearch, Docker and Prometheus/Loki orbit a root coordinator that runs them in
parallel and correlates what they find. Built on [Google ADK](https://google.github.io/adk-docs/),
open source under MIT.

<figure markdown>
  ![The Orrery web console after a full incident triage. The agent reports a Critical verdict: Kafka and Elasticsearch healthy, a TargetDown alert firing in Alertmanager, two unhealthy Docker containers, and the Kubernetes check unverified. The side panel keeps the recorded verdict and the per-system findings behind it.](images/web-console-triage.png)
  <figcaption>One question, five systems checked in parallel, one verdict — with the evidence for it kept alongside.</figcaption>
</figure>

## Pick your path

<div class="grid cards" markdown>

-   :material-play-circle:{ .lg .middle } __I want to try it__

    ---

    Run the full stack locally in Docker in under 5 minutes — Kafka, Postgres, Prometheus, and the orchestrator ready to go.

    [:octicons-arrow-right-24: Quick start with Docker](getting-started.md#quick-start-docker-no-clone-required)

-   :material-rocket-launch:{ .lg .middle } __I want to deploy it__

    ---

    Helm chart, multi-replica Postgres sessions, HPA, rolling updates, and observability scrape targets.

    [:octicons-arrow-right-24: Production deployment](deployment.md)

-   :material-hammer-wrench:{ .lg .middle } __I want to extend it__

    ---

    Add your own specialist agent, wire it into the orchestrator, and ship it behind the same RBAC + guardrails.

    [:octicons-arrow-right-24: Adding a new agent](adding-an-agent.md)

-   :material-chat:{ .lg .middle } __I want a chat surface__

    ---

    Bring the agents into Slack or Google Chat with interactive Approve / Deny cards and email/user-ID RBAC.

    [:octicons-arrow-right-24: Integrations overview](integrations.md)

</div>

---

## Architecture Overview

The platform follows a **Coordinator-Specialist** pattern. A root orchestrator analyzes user intent and delegates to specialized agents. Cross-cutting concerns like safety, observability, and resilience are handled globally via a plugin system.

```mermaid
graph LR
    subgraph Frontends
        direction TB
        WEB[Web UI / CLI]
        SLACK[Slack]
        GCHAT[Google Chat]
    end

    subgraph Orchestrator
        ROOT[Orrery Chat]
    end

    subgraph Specialists
        direction TB
        KAFKA[Kafka Agent]
        K8S[K8s Agent]
        OBS[Observability]
        ES[Elasticsearch]
        DOCKER[Docker Agent]
        JOURNAL[Ops Journal]
        TRIAGE[Incident Triage]
    end

    subgraph Plugins
        direction TB
        P1[RBAC & Guardrails]
        P2[Metrics & Audit]
        P3[Memory & Resilience]
    end

    WEB --> ROOT
    SLACK --> ROOT
    GCHAT --> ROOT

    ROOT --> KAFKA
    ROOT --> K8S
    ROOT --> OBS
    ROOT --> ES
    ROOT --> DOCKER
    ROOT --> JOURNAL
    ROOT --> TRIAGE

    ROOT -.-> P1
    ROOT -.-> P2
    ROOT -.-> P3
```

---

## Jump to a topic

<div class="grid cards" markdown>

-   :material-view-list:{ .lg .middle } __[Agents overview](agents-overview.md)__

    ---

    What's in the box — every agent, its tools, and the role each tool requires.

-   :material-shield-lock:{ .lg .middle } __[Guardrails & RBAC](guardrails.md)__

    ---

    Three risk tiers, three roles, and how the confirmation gate works end-to-end.

-   :material-key-variant:{ .lg .middle } __[Security & auth](config/security.md)__

    ---

    JWT-authenticated HTTP front door, claim-to-role mapping, and the `SecretsManager` for mounted Kubernetes Secrets.

-   :material-brain:{ .lg .middle } __[Cross-session memory](memory.md)__

    ---

    Let agents recall past incidents, resolutions, and team preferences.

-   :material-book-open-variant:{ .lg .middle } __[Knowledge retrieval](knowledge.md)__

    ---

    Index your runbooks, postmortems and ADRs so the agent answers from what your team wrote — with a citation and a document age.

-   :material-chart-bar:{ .lg .middle } __[Observability](metrics.md)__

    ---

    Prometheus metrics, OpenTelemetry traces, and log↔trace correlation — with a one-command Grafana stack.

-   :material-fire-alert:{ .lg .middle } __[Runbooks](runbooks/README.md)__

    ---

    Operating Orrery itself: the first five minutes, escalation, and a page per alert.

-   :material-lifebuoy:{ .lg .middle } __[Troubleshooting](troubleshooting.md)__

    ---

    Common errors across every surface with pointers to the fix.

</div>

---

## Core Philosophy

1.  **Safety First:** No destructive tool executes without verified human confirmation.
2.  **Autonomous Investigation:** Agents run diagnostics in parallel, mimicking an SRE's thought process.
3.  **Closed-Loop Remediation:** Actions are always followed by verification and retry loops.
4.  **Observable by Design:** Every interaction is instrumented with Prometheus metrics, OpenTelemetry traces, and audit logs.

---

## Project Structure

| Component | Path | Description |
|-----------|------|-------------|
| [**core**](core/README.md) | `core/` | Shared library: agent factories, plugin system, validation, and base configurations. |
| [**agents**](agents/orrery-assistant.md) | `agents/` | Specialist agent implementations (Kafka, K8s, Docker, etc.). |
| [**infra**](config/general.md#infrastructure) | `infra/` | Local diagnostic stack (Prometheus, Loki, Kafka, Grafana). |
| [**roadmap**](enhancements/README.md) | `docs/enhancements/` | Ongoing development and enhancement proposals (AEP). |


---

## Background: how this was built

The reference pages above describe *what* Orrery does. **[Building AI Agents for DevOps](https://tirraflow.com/series/building-ai-agents-for-devops)**
is a twelve-part series on *why* it does it that way — the decisions, the dead ends, and the
rewrites behind each subsystem. Every part maps onto a page in these docs.

| Post | Reference page |
|---|---|
| [1 · Why and how I designed the architecture](https://tirraflow.com/posts/building-ai-agents-for-devops-part-1-why-and-how-i-designed-the-architecture) | [Design patterns](agent-design-patterns.md) · [Agents overview](agents-overview.md) |
| [2 · From terminal to Slack](https://tirraflow.com/posts/building-ai-agents-for-devops-part-2-from-terminal-to-slack) | [Slack](integrations/slack.md) |
| [3 · RBAC — who can do what](https://tirraflow.com/posts/building-ai-agents-for-devops-part-3-rbac-who-can-do-what) | [ADR-001: RBAC](adr/001-rbac.md) · [Guardrails & RBAC](guardrails.md) |
| [4 · Prometheus metrics](https://tirraflow.com/posts/building-ai-agents-for-devops-part-4-prometheus-metrics) | [Observability](metrics.md) |
| [5 · Sub-agents vs AgentTool](https://tirraflow.com/posts/building-ai-agents-for-devops-part-5-sub-agents-vs-agenttool) | [ADR-002: Agent composition](adr/002-agent-tool-vs-sub-agents.md) |
| [6 · Security hardening](https://tirraflow.com/posts/building-ai-agents-for-devops-part-6-security-hardening) | [Security](config/security.md) · [Guardrails](guardrails.md) |
| [7 · ADK plugins and async tools](https://tirraflow.com/posts/building-ai-agents-for-devops-part-7-adk-plugins-and-async-tools) | [Design patterns](agent-design-patterns.md) |
| [8 · Agent evaluations](https://tirraflow.com/posts/building-ai-agents-for-devops-part-8-agent-evaluations) | [Evaluations](evals.md) |
| [9 · Google Chat integration](https://tirraflow.com/posts/building-ai-agents-for-devops-part-9-google-chat-integration) | [Google Chat](integrations/google-chat.md) |
| [10 · Fixing the approve handshake across sub-agents](https://tirraflow.com/posts/building-ai-agents-for-devops-part-10-fixing-the-approve-handshake-across-sub) | [Guardrails & confirmation](guardrails.md) |
| [11 · Adding planning mode with ADK planners](https://tirraflow.com/posts/building-ai-agents-for-devops-part-11-adding-planning-mode-with-adk-planners) | [Design patterns](agent-design-patterns.md) |
| [12 · Distributed tracing with OpenTelemetry](https://tirraflow.com/posts/building-ai-agents-for-devops-part-12-distributed-tracing-with-opentelemetry) | [Observability](metrics.md) |

Two standalone pieces go wider than the series:
[The agent gateway](https://tirraflow.com/posts/the-agent-gateway-one-turn-pipeline-behind-every-surface) on how five
surfaces share one gate, and
[How to build AI agents](https://tirraflow.com/posts/how-to-build-ai-agents-a-practical-guide-best-practices) on the
patterns that generalise beyond this codebase.
