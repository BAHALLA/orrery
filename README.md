<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/orrery-mark-dark.svg">
    <img src="docs/assets/brand/orrery-mark-light.svg" alt="Orrery" width="112" height="112">
  </picture>
</p>

<h1 align="center">Orrery</h1>

<p align="center"><strong>SRE agents you can let near production.</strong></p>

<p align="center">
  Autonomous DevOps &amp; SRE agents that investigate, correlate and remediate —<br>
  with a human gate on every destructive action.
</p>

<p align="center">
  <a href="https://github.com/BAHALLA/orrery/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/BAHALLA/orrery/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/BAHALLA/orrery/releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/BAHALLA/orrery?display_name=tag&sort=semver&color=2563eb"></a>
  <a href="https://github.com/BAHALLA/orrery/pkgs/container/orrery"><img alt="Container image" src="https://img.shields.io/badge/ghcr.io-bahalla%2Forrery-0f172a?logo=docker&logoColor=white"></a>
  <a href="https://bahalla.github.io/orrery/"><img alt="Docs" src="https://img.shields.io/badge/docs-bahalla.github.io%2Forrery-2563eb"></a>
  <a href="https://www.python.org/downloads/release/python-3140/"><img alt="Python 3.14+" src="https://img.shields.io/badge/python-3.14+-3776ab?logo=python&logoColor=white"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/github/license/BAHALLA/orrery?color=0f172a"></a>
</p>

<p align="center">
  <a href="https://bahalla.github.io/orrery/getting-started/">Get started</a> ·
  <a href="https://bahalla.github.io/orrery/">Documentation</a> ·
  <a href="https://bahalla.github.io/orrery/deployment/">Deploy</a> ·
  <a href="https://bahalla.github.io/orrery/adding-an-agent/">Build an agent</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<br>

<p align="center">
  <img src="docs/images/web-console-triage.png" alt="The Orrery web console after a full incident triage: a Critical verdict, per-system findings for Kafka, Elasticsearch, Alertmanager, Docker and Kubernetes, and the evidence kept in a side panel." width="900">
</p>
<p align="center"><sub>One question, five systems checked in parallel, one verdict — with the evidence kept alongside.</sub></p>

## Why Orrery

An alert tells you *that* something is wrong. Finding out *what* still means a human
opening five consoles at 03:00. Orrery gives you specialist agents — Kafka, Kubernetes,
Elasticsearch, Docker, Prometheus/Loki — coordinated by a root agent that runs them in
parallel, correlates what they find, and proposes or performs the fix.

The name is the design: an orrery is a mechanical model of the solar system. Specialists
orbit a coordinator, and every moving part is visible and accountable.

What makes it safe enough to point at production:

- **A human gate on every mutation.** Mutating and destructive tools stop and ask. Approval is
  *requester-verified* — a deliberate word, from the same signed-in person who asked, after
  the action existed. A casual "ok" or someone else's "approve" is refused.
- **Two orthogonal axes of control.** RBAC answers *who* (viewer / operator / admin); an autonomy
  level answers *what this process may do* (L2 read-only → L4 confirmed-destructive).
- **Prompt-injection screening in both directions.** An injected user message is blocked before
  the model runs; injected text arriving *inside a tool result* (a pod annotation, a log line) is
  neutralized in place, because that payload is also the evidence being diagnosed.
- **An audit trail you can alert on.** Every tool call, and every confirmation raised, decided,
  refused or expired, is a structured log line and a Prometheus counter. Someone else trying to
  approve your action pages on-call.

## Quick start

```bash
docker run --rm -p 8000:8000 -e GOOGLE_API_KEY=your-key ghcr.io/bahalla/orrery:latest
```

Open <http://localhost:8000>. That is the web console with in-memory sessions and whichever
provider you configured — Gemini, Claude, OpenAI, or a local Ollama model (no key needed).

For the full local stack (Kafka, Postgres, Prometheus, Loki, Alertmanager, Elasticsearch) —
still no clone required:

```bash
curl -O https://raw.githubusercontent.com/BAHALLA/orrery/main/docker-compose.yml
GOOGLE_API_KEY=your-key docker compose --profile demo up -d
```

From source: `make install && make up && make run-api`, then `make dev-token ROLE=admin` for a
token to sign in with. `make help` lists every target. The
[Getting Started guide](https://bahalla.github.io/orrery/getting-started/) covers providers,
SSO, Slack and Google Chat.

## What's in the box

| Agent | Tools | What it does |
|---|---:|---|
| [**orrery-assistant**](https://bahalla.github.io/orrery/agents/orrery-assistant/) | — | Root coordinator; single-turn incident triage; closed-loop remediation (act → verify → retry) |
| [**kafka-health**](https://bahalla.github.io/orrery/agents/kafka-health/) | 24 | Brokers, topics, consumer lag, offset resets; Strimzi-aware |
| [**k8s-health**](https://bahalla.github.io/orrery/agents/k8s-health/) | 26 | Pods, deployments, events, logs, rollbacks, resource usage; operator-aware |
| [**elasticsearch**](https://bahalla.github.io/orrery/agents/elasticsearch/) | 24 | Cluster, index and shard diagnostics, ILM, snapshots; ECK-aware |
| [**observability**](https://bahalla.github.io/orrery/agents/observability/) | 16 | Prometheus queries, Loki logs, Alertmanager silences |
| [**docker-agent**](https://bahalla.github.io/orrery/agents/docker-agent/) | 17 | Container health, stats, logs, Compose projects |
| [**ops-journal**](https://bahalla.github.io/orrery/agents/ops-journal/) | 10 | Notes, preferences and bookmarks that persist across sessions |
| [**slack-bot**](https://bahalla.github.io/orrery/agents/slack-bot/) / [**google-chat-bot**](https://bahalla.github.io/orrery/agents/google-chat-bot/) | — | Chat surfaces with Approve / Deny cards, same gate and RBAC |

Every agent runs standalone or composed under the coordinator, and every cross-cutting concern
is a plugin applied once to all of them.

## Highlights

- **Reads your runbooks, not just your clusters** — opt-in retrieval over the docs your team wrote
  (filesystem, git, Confluence → Elasticsearch BM25 or hybrid pgvector). Every passage cites its
  source and its age. [Knowledge retrieval →](https://bahalla.github.io/orrery/knowledge/)
- **Deterministic triage workflow** — a graph `Workflow` fans out to all specialists, joins, records a
  severity verdict, then routes to bounded remediation or a final report. Runs on a schedule with
  `make run-triage`. [ADR-003 →](https://bahalla.github.io/orrery/adr/003-graph-workflow-inversion/)
- **Cross-session memory** — past incidents and resolutions are recalled on demand, with credentials
  scrubbed on the way in and out. [Memory →](https://bahalla.github.io/orrery/memory/)
- **Long incidents don't hit the context wall** — older turns are compacted into a digest for the
  model while the full record stays intact for audit and replay.
- **Any LLM provider** — Gemini, Claude, OpenAI or Ollama via one env var; context caching and
  safety settings applied automatically on Gemini.
- **Five surfaces, one gate** — web console (OIDC/PKCE SSO or JWT), CLI, ADK dev UI, Slack, Google
  Chat. The confirmation and RBAC rules are identical on every one.
- **Observable by design** — Prometheus metrics, OpenTelemetry traces with log↔trace correlation,
  and a `runbook_url` on every alert rule pointing at an
  [on-call runbook](https://bahalla.github.io/orrery/runbooks/) for operating Orrery itself.
- **Production deployment** — Helm chart with HPA, PDB, network policies, Postgres-backed sessions and
  approvals, signed multi-arch images with SBOMs. [Deployment →](https://bahalla.github.io/orrery/deployment/)

## How it fits together

Built on [Google ADK](https://google.github.io/adk-docs/). A chat-mode root agent owns the
conversation and delegates to specialists as tools; a separate graph workflow provides the
deterministic batch path. Guardrails, RBAC, autonomy, audit, metrics, tracing, injection screening,
credential redaction and output capping are ADK plugins registered once on the runner, so a new
agent inherits all of them by construction.

Read the [architecture overview](https://bahalla.github.io/orrery/agent-design-patterns/), the
[ADRs](https://bahalla.github.io/orrery/adr/001-rbac/), or the
[enhancement proposals](https://bahalla.github.io/orrery/enhancements/) that record how each
piece came to be.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go
through [SECURITY.md](SECURITY.md), not the public tracker. `make check` runs the whole gate
(lint, types, ~1,400 Python tests, web console tests) and mirrors CI.

## License

[MIT](LICENSE).
