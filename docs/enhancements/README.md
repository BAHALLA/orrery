# Agent Enhancement Proposals (AEP)

This directory contains enhancement proposals for the Orrery DevOps platform.
Each proposal documents a gap identified by comparing the current implementation
against the [Google ADK documentation](https://google.github.io/adk-docs/) and
enterprise-grade requirements for autonomous DevOps systems.

## Priority Matrix

| Priority | AEP | Title | Status | Effort | Impact |
|----------|-----|-------|--------|--------|--------|
| <span class="badge badge--red">P0</span> | [AEP-001](aep-001-adk-native-confirmation.md) | ADK-Native Tool Confirmation | <span class="badge badge--green">completed</span> | Medium | High |
| <span class="badge badge--red">P0</span> | [AEP-002](aep-002-agent-evaluation.md) | Agent Evaluation Framework | <span class="badge badge--green">completed</span> | High | Critical |
| <span class="badge badge--red">P0</span> | [AEP-003](aep-003-memory-service.md) | Cross-Session Memory Service | <span class="badge badge--green">completed</span> | Medium | High |
| <span class="badge badge--red">P0</span> | [AEP-011](aep-011-deployment-hardening.md) | Production Deployment Hardening | <span class="badge badge--green">completed</span> | High | Critical |
| <span class="badge badge--red">P0</span> | [AEP-013](aep-013-security-hardening.md) | Security Hardening & Auth Layer | <span class="badge badge--green">completed</span> | High | Critical |
| <span class="badge badge--red">P0</span> | [AEP-014](aep-014-supply-chain-security.md) | Supply Chain Security (SBOM, Signing, Scan) | <span class="badge badge--green">completed</span> | Medium | High |
| <span class="badge badge--amber">P1</span> | [AEP-004](aep-004-loop-agent-remediation.md) | LoopAgent for Self-Healing Remediation | <span class="badge badge--green">completed</span> | Medium | High |
| <span class="badge badge--amber">P1</span> | [AEP-007](aep-007-context-caching.md) | Context Caching for LLM Cost Reduction | <span class="badge badge--green">completed</span> | Low | High |
| <span class="badge badge--amber">P1</span> | [AEP-010](aep-010-observability-tracing.md) | Distributed Tracing & Observability | <span class="badge badge--green">completed</span> | Medium | High |
| <span class="badge badge--amber">P1</span> | [AEP-015](aep-015-cost-observability.md) | Cost Observability & Per-Tenant Budgets | <span class="badge badge--amber">proposed</span> | Medium | High |
| <span class="badge badge--amber">P1</span> | [AEP-017](aep-017-runbooks-oncall.md) | Runbooks & On-Call Documentation | <span class="badge badge--green">completed</span> | Low | High |
| <span class="badge badge--amber">P1</span> | [AEP-020](aep-020-context-compaction.md) | Conversation Context Compaction | <span class="badge badge--green">completed</span> | Low | High |
| <span class="badge badge--amber">P1</span> | [AEP-021](aep-021-provider-fallback.md) | LLM Provider Fallback Chain | <span class="badge badge--amber">proposed</span> | Low-Medium | High |
| <span class="badge badge--amber">P1</span> | [AEP-024](aep-024-approval-audit-events.md) | Approval Audit Events | <span class="badge badge--green">completed</span> | Low | Medium-High |
| <span class="badge badge--amber">P1</span> | [AEP-025](aep-025-knowledge-retrieval.md) | Pluggable Knowledge Retrieval | <span class="badge badge--green">completed</span> | High | High |
| <span class="badge badge--blue">P2</span> | [AEP-022](aep-022-trajectory-capture.md) | Trajectory Capture & Eval Harvesting | <span class="badge badge--amber">proposed</span> | Medium | Medium-High |
| <span class="badge badge--blue">P2</span> | [AEP-023](aep-023-scheduled-tasks.md) | First-Class Scheduled Agent Tasks | <span class="badge badge--amber">proposed</span> | Medium | Medium |
| <span class="badge badge--blue">P2</span> | [AEP-026](aep-026-experience-capture-rex.md) | Experience Capture & REX Generation | <span class="badge badge--amber">proposed</span> | Medium-High | Medium-High |
| <span class="badge badge--blue">P2</span> | [AEP-005](aep-005-a2a-protocol.md) | Agent-to-Agent (A2A) Protocol Support | <span class="badge badge--amber">proposed</span> | High | High |
| <span class="badge badge--blue">P2</span> | [AEP-006](aep-006-artifacts.md) | Artifact Management for Reports & Logs | <span class="badge badge--amber">proposed</span> | Low | Medium |
| <span class="badge badge--blue">P2</span> | [AEP-008](aep-008-skills.md) | Skills-Based Tool Organization | <span class="badge badge--amber">proposed</span> | Medium | Medium |
| <span class="badge badge--blue">P2</span> | [AEP-009](aep-009-streaming.md) | Streaming & Real-Time Agent Responses | <span class="badge badge--amber">proposed</span> | High | Medium |
| <span class="badge badge--blue">P2</span> | [AEP-016](aep-016-load-chaos-testing.md) | Load & Chaos Testing Harness | <span class="badge badge--amber">proposed</span> | Medium | Medium |
| <span class="badge badge--blue">P2</span> | [AEP-019](aep-019-web-console.md) | Web Console for Onboarding & Operator Usage | <span class="badge badge--green">completed</span> | High | Medium-High |
| <span class="badge badge--grey">P3</span> | [AEP-012](aep-012-custom-agents.md) | Custom Agent Classes for DevOps Patterns | <span class="badge badge--amber">proposed</span> | Medium | Medium |
| <span class="badge badge--red">P0</span> | [AEP-018](aep-018-pubsub-idempotency-hpa.md) | Pub/Sub Worker Idempotency & Backlog-Based HPA | <span class="badge badge--green">completed</span> | Medium | High |

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
and the deployment + security perimeter has now landed. **Phase 1 is
complete**:

- **AEP-011 ✅**: Kubernetes manifests, Helm chart, CD pipeline, rate limiting, PostgreSQL sessions *(completed 2026-04-11)*
- **AEP-013 ✅**: JWT/OAuth authentication, PII redaction, prompt injection detection, Gemini safety filters, secrets management *(completed 2026-07-18)*
- **AEP-014 ✅**: SBOM generation, cosign image signing, trivy scanning, base image pinning, admission policy *(completed 2026-07-18)*
- **AEP-018 ✅**: Pub/Sub worker idempotency (dedup on Chat `eventId`) and backlog-based HPA to remove the single-replica SPOF in the Chat transport

### Phase 2 - Autonomous Capabilities & Observability (P1)

Self-healing loops, cost control, distributed tracing, and on-call docs
make agents truly autonomous and operable in production:

- **AEP-004 ✅**: LoopAgent for detect → remediate → verify → repeat workflows
- **AEP-007 ✅**: Context caching to reduce LLM costs (low effort, high impact)
- **AEP-010 ✅**: OpenTelemetry distributed tracing across agent calls *(completed 2026-06-21)*
- **AEP-015**: Per-tenant LLM cost tracking and budget alerts
- **AEP-017 ✅**: On-call runbooks + `runbook_url` on every alert *(completed 2026-08-22)*
- **AEP-020 ✅**: Conversation context compaction so long incident sessions don't overflow the model window *(completed 2026-07-25)*
- **AEP-021**: LLM provider fallback chain so a provider outage/quota isn't a full platform outage
- **AEP-024 ✅**: Approval audit events — four lifecycle events, counters, and a critical alert on non-requester approval attempts *(completed 2026-08-22)*
- **AEP-025 ✅**: Pluggable knowledge retrieval — two seams, Elasticsearch/BM25 and hybrid pgvector backends, filesystem/git/Confluence sources *(completed 2026-08-22; retrieval-quality evals remain)*

### Phase 3 - Extended Features (P2)

Cross-platform agent communication, artifact management, streaming,
tool organization, and load/chaos coverage:

- **AEP-005**: A2A protocol for cross-platform agent communication
- **AEP-006**: Artifact storage for incident reports and triage snapshots
- **AEP-008**: Skills-based tool grouping for cleaner agent composition
- **AEP-009**: Streaming responses for real-time agent output
- **AEP-016**: Load and chaos testing harness (Locust, LLM flakiness, circuit breaker exercises)
- **AEP-019 ✅**: Web console for onboarding and safe operator usage (chat + tool timeline, confirmation UI, triage view, onboarding wizard) — sits behind the AEP-013 auth perimeter; token streaming tracked as AEP-009
- **AEP-022**: Trajectory capture — harvest real runs into eval scenarios (and a fine-tune corpus)
- **AEP-023**: First-class scheduled agent tasks — recurring triage sweeps with persisted run history
- **AEP-026**: Experience capture & REX — mine the platform's own incident history into reviewed runbooks

> **AEP-020 – 023** were identified by benchmarking Orrery against the mature
> [Hermes agent architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture):
> context compaction, provider fallback, trajectory capture, and first-class
> scheduling were the patterns worth adapting to Orrery's ADK/Postgres model.

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
| 2026-07-12 | AEP-019 added (P2) | Onboarding/usage gap: the only browser surface today is ADK's developer Dev UI. A product web console accelerates adoption but is UX, not a production blocker — sequenced behind the AEP-013/014 security perimeter it would amplify, and alongside streaming (AEP-009). |
| 2026-07-12 | AEP-019: proposed → in-progress | Milestone 1 (authenticated chat console) shipped: Vite + React SPA under web/, served by the FastAPI front door behind ORRERY_WEB_CONSOLE_ENABLED, with two-stage Docker build and a dedicated web CI job. |
| 2026-07-18 | AEP-013: in-progress → completed | Remaining content-level defenses shipped: SafetyScreenPlugin (prompt-injection screen, blocks in before_run), PIIRedactionPlugin (credential scrubbing of tool results, registered before audit), and Gemini safety filters in create_agent — all on by default with env off-switches. |
| 2026-07-18 | AEP-014: proposed → completed | Chain of custody closed: digest-pinned base images, CycloneDX Python SBOM on CI builds + releases, Trivy image scan gating releases with SARIF to code scanning, PR dependency-review gate, opt-in Sigstore admission policy. Cosign signing and buildx SBOM/provenance had already landed with AEP-011. |
| 2026-07-19 | AEP-019: Milestone 1 complete | Tool-call timeline (GET /session/{id}/activity over ActivityPlugin's session_log, owner-scoped) and confirmation UI (GET /confirmations/pending + Approve/Deny panel sending the literal decision words through POST /chat) landed — the console now renders the orchestration and the guarded-action handshake, with the requester-verified gate unchanged as the sole approval authority. Milestones 2–3 (triage view, onboarding wizard) remain. |
| 2026-07-25 | AEP-020: proposed → completed | Delivered by configuring ADK's native `EventsCompactionConfig` (ADK ≥ 1.16) rather than the hand-rolled `ContextEngine`/plugin the AEP originally specified. Native compaction also avoids splitting a `function_call` from its `function_response` — a bug the proposed slice-based design would have had — and preserves the original events by construction, so lineage needed no custom state. Effort dropped Medium → Low. |
| 2026-07-19 | AEP-019: Milestone 2 triage view shipped | Run-triage button → incident_triage_agent; GET /session/{id}/triage exposes the recorded incident_severity + triage_report (AgentTool forwards sub-session state deltas to the parent session, verified against ADK 2.4); severity banner with collapsible markdown report; timeline polled every 2.5s while a request is in flight. Remaining for M2: per-system status chips and the remediation trace (batch-workflow-only today). |
| 2026-08-22 | AEP-024 added (P1) | Audit review: the confirmation gate enforces requester-verified approval correctly but records no approval event. Refused approvals — a second person trying to approve someone else's destructive action — leave no trace at all. |
| 2026-08-22 | AEP-025 added (P1) | Largest remaining capability gap: the agent reads live infrastructure through ~70 tools but cannot read anything a human wrote. No runbooks, postmortems or indexed docs exist, and memory recall is lexical. Scoped as two seams (sources, retrieval backends) so neither a document store nor a search vendor is hard-wired. |
| 2026-08-22 | AEP-026 added (P2) | The platform records every session, tool outcome and triage verdict and never reads them in aggregate. Sequenced behind AEP-025, which gives generated REX documents somewhere to land. |
| 2026-08-22 | AEP-017: proposed → completed | Eleven runbooks and `runbook_url` on all ten alert rules. Unblocked by AEP-025: the corpus now has somewhere to be retrieved from. One planned alert was dropped rather than shipped broken — prompt-injection detection exports no metric, so it cannot be alerted on today. |
| 2026-08-22 | AEP-025: proposed → completed | Phase 1 (seams, chunking, sync, Elasticsearch/BM25, `search_knowledge` on the tool path) then phase 2 (provider-agnostic `resolve_embedder()`, a hybrid pgvector backend fusing semantic and lexical ranks because pure vector search misses exact identifiers, and a Confluence source that refuses to auto-discover spaces). Retrieval-quality evals remain the one open acceptance criterion. |
| 2026-08-22 | AEP-024: proposed → completed | Four confirmation lifecycle events on the audit stream, four counters and a decision-latency histogram, plus a critical alert on non-requester approval attempts. No `confirmation_id` column was needed — `action_id` was already a uuid primary key. |
