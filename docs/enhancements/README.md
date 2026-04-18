# Agent Enhancement Proposals (AEP)

This directory contains enhancement proposals for the AI Agents DevOps platform.
Each proposal documents a gap identified by comparing the current implementation
against the [Google ADK documentation](https://google.github.io/adk-docs/) and
enterprise-grade requirements for autonomous DevOps systems.

## Priority Matrix

| Priority | AEP | Title | Status | Effort | Impact |
|----------|-----|-------|--------|--------|--------|
| P0 | [AEP-001](aep-001-adk-native-confirmation.md) | ADK-Native Tool Confirmation | **completed** | Medium | High |
| P0 | [AEP-002](aep-002-agent-evaluation.md) | Agent Evaluation Framework | **completed** | High | Critical |
| P0 | [AEP-003](aep-003-memory-service.md) | Cross-Session Memory Service | **completed** | Medium | High |
| P0 | [AEP-011](aep-011-deployment-hardening.md) | Production Deployment Hardening | **completed** | High | Critical |
| P0 | [AEP-013](aep-013-security-hardening.md) | Security Hardening & Auth Layer | proposed | High | Critical |
| P0 | [AEP-014](aep-014-supply-chain-security.md) | Supply Chain Security (SBOM, Signing, Scan) | proposed | Medium | High |
| P1 | [AEP-004](aep-004-loop-agent-remediation.md) | LoopAgent for Self-Healing Remediation | **completed** | Medium | High |
| P1 | [AEP-007](aep-007-context-caching.md) | Context Caching for LLM Cost Reduction | **completed** | Low | High |
| P1 | [AEP-010](aep-010-observability-tracing.md) | Distributed Tracing & Observability | proposed | Medium | High |
| P1 | [AEP-015](aep-015-cost-observability.md) | Cost Observability & Per-Tenant Budgets | proposed | Medium | High |
| P1 | [AEP-017](aep-017-runbooks-oncall.md) | Runbooks & On-Call Documentation | proposed | Low | High |
| P2 | [AEP-005](aep-005-a2a-protocol.md) | Agent-to-Agent (A2A) Protocol Support | proposed | High | High |
| P2 | [AEP-006](aep-006-artifacts.md) | Artifact Management for Reports & Logs | proposed | Low | Medium |
| P2 | [AEP-008](aep-008-skills.md) | Skills-Based Tool Organization | proposed | Medium | Medium |
| P2 | [AEP-009](aep-009-streaming.md) | Streaming & Real-Time Agent Responses | proposed | High | Medium |
| P2 | [AEP-016](aep-016-load-chaos-testing.md) | Load & Chaos Testing Harness | proposed | Medium | Medium |
| P3 | [AEP-012](aep-012-custom-agents.md) | Custom Agent Classes for DevOps Patterns | proposed | Medium | Medium |
| P0 | [AEP-018](aep-018-pubsub-idempotency-hpa.md) | Pub/Sub Worker Idempotency & Backlog-Based HPA | proposed | Medium | High |

## How to Read These Proposals

Each AEP follows a consistent structure:

- **Status**: `proposed` | `accepted` | `in-progress` | `completed`
- **Priority**: P0 (do first) through P3 (future)
- **Gap Analysis**: What's missing vs. what ADK provides
- **Proposed Solution**: How to implement it
- **Affected Files**: Which files need changes
- **Dependencies**: Other AEPs or external requirements
- **Acceptance Criteria**: Definition of done

## Roadmap

### Phase 1 - Production Readiness (P0)

The platform has strong foundations (RBAC, guardrails, metrics, audit, memory)
and the deployment layer has now landed. The remaining P0 work is the
security perimeter:

- **AEP-011 ✅**: Kubernetes manifests, Helm chart, CD pipeline, rate limiting, PostgreSQL sessions *(completed 2026-04-11)*
- **AEP-013**: JWT/OAuth authentication, PII redaction, prompt injection detection, secrets management
- **AEP-014**: SBOM generation, cosign image signing, trivy scanning, base image pinning
- **AEP-018**: Pub/Sub worker idempotency (dedup on Chat `eventId`) and backlog-based HPA to remove the single-replica SPOF in the Chat transport

### Phase 2 - Autonomous Capabilities & Observability (P1)

Self-healing loops, cost control, distributed tracing, and on-call docs
make agents truly autonomous and operable in production:

- **AEP-004 ✅**: LoopAgent for detect → remediate → verify → repeat workflows
- **AEP-007 ✅**: Context caching to reduce LLM costs (low effort, high impact)
- **AEP-010**: OpenTelemetry distributed tracing across agent calls
- **AEP-015**: Per-tenant LLM cost tracking and budget alerts
- **AEP-017**: Runbooks for common incidents (circuit breaker open, loop storm, session DB full)

### Phase 3 - Extended Features (P2)

Cross-platform agent communication, artifact management, streaming,
tool organization, and load/chaos coverage:

- **AEP-005**: A2A protocol for cross-platform agent communication
- **AEP-006**: Artifact storage for incident reports and triage snapshots
- **AEP-008**: Skills-based tool grouping for cleaner agent composition
- **AEP-009**: Streaming responses for real-time agent output
- **AEP-016**: Load and chaos testing harness (Locust, LLM flakiness, circuit breaker exercises)

### Phase 4 - Advanced Patterns (P3)

Custom agent classes for domain-specific DevOps patterns:

- **AEP-012**: Custom agent subclasses (DiagnosticAgent, RemediationAgent, etc.)

## Priority Changes Log

| Date | Change | Reason |
|------|--------|--------|
| 2026-04-08 | AEP-011: P2 → P0 | Audit: deployment is the top blocker for production. Health probes and graceful shutdown already done; remaining work (K8s, CD, Helm) is critical. |
| 2026-04-08 | AEP-013: P3 → P0 | Audit: no authentication makes RBAC meaningless. Web UI has zero auth — anyone on network gets access. |
| 2026-04-08 | AEP-005: P1 → P2 | A2A protocol is a feature, not a production requirement. Deprioritized behind security and deployment. |
| 2026-04-08 | AEP-006: P1 → P2 | Artifacts are useful but not a blocker. Deprioritized behind tracing and cost control. |
| 2026-04-08 | AEP-010: P2 → P1 | Distributed tracing is essential for debugging multi-agent flows in production. |
| 2026-04-11 | AEP-011: in-progress → completed | K8s manifests, Helm chart, CD pipeline, rate limiting, Postgres session support all landed. |
| 2026-04-11 | AEP-014 added (P0) | Supply chain security (SBOM/cosign/trivy) is table-stakes for enterprise deployment — split out of AEP-011 follow-ups. |
| 2026-04-11 | AEP-015 added (P1) | Cost observability was flagged in production readiness audit but had no AEP. Needed before scale-up. |
| 2026-04-11 | AEP-016 added (P2) | Load/chaos testing gap identified during AEP-011 review. |
| 2026-04-11 | AEP-017 added (P1) | Runbooks are required before on-call rotation; gap in existing docs. |
| 2026-04-18 | AEP-018 added (P0) | Pub/Sub at-least-once delivery means redelivered events can double-act on `@destructive` tools; single-replica worker is a SPOF during incidents. Split out of Google Chat Pub/Sub transport work. |
