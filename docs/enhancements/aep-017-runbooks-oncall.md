# AEP-017: Runbooks & On-Call Documentation

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--green">completed</span> |
| **Priority** | <span class="badge badge--amber">P1</span> |
| **Effort** | Low (2-3 days) |
| **Impact** | High |
| **Dependencies** | AEP-010 (tracing), AEP-011 (completed), AEP-015 (cost alerts) |

> **Completed 2026-08-22.** Eleven pages under `docs/runbooks/`, and a
> `runbook_url` annotation on all ten Prometheus rules. Two deviations from the
> plan below, both deliberate:
>
> - The runbook set is organised around *which system is actually broken* —
>   Orrery, or something Orrery is correctly reporting on. That distinction
>   turned out to be the first question in nearly every scenario, so it is a
>   fixed section in the template rather than prose inside each page.
> - `OrreryPromptInjectionDetected` was **not** shipped as an alert at first. It
>   could not fire: `SafetyScreenPlugin` exported no metric and `MetricsPlugin`
>   bounds the `status` label to four values, so a `BLOCKED` result recorded as
>   `ok`. Shipping an alert that can never fire would be worse than not having one.
>
> **Update:** the same day (commit `4f397f1`), the screen gained a metric,
> `orrery_safety_screen_total` (labelled by `direction` and `source`), and two
> alerts on it: `OrreryDirectInjectionAttempts` and
> `OrreryIndirectInjectionDetected`. Detection is no longer log-only. As of
> 2026-10-23 there are fourteen pages under `docs/runbooks/` (including
> README, template, checklist and escalation) and a `runbook_url` on all
> fifteen alert rules.
>
> The runbooks are indexed by `make knowledge-sync`
> ([AEP-025](aep-025-knowledge-retrieval.md)), so the agent retrieves them
> through `search_knowledge` — which was the dependency that made writing them
> the next step.

## Gap Analysis

### Current Implementation

The project has excellent architectural documentation:

- `CLAUDE.md` — design patterns and architecture
- `docs/adr/` — architectural decision records (ADR-001 RBAC, ADR-002 delegation patterns)
- `docs/getting-started.md` — local setup
- `docs/deployment.md` — production install (AEP-011)
- `docs/metrics.md` — observability reference

What's missing is anything aimed at **3 AM on-call**:

- No runbook directory
- No mapping from Prometheus alert name → remediation steps
- No incident response checklist
- No escalation policy
- No documented recovery for operational failure modes

The existing alerting rules (`infra/alert_rules.yml`) fire alerts with
only a summary — there's no `runbook_url` annotation pointing to
"what do I do about this?"

### Why this matters

A platform that touches production infrastructure (restarts pods,
rolls back deployments, silences alerts) is itself oncallable. When
the orrery-assistant's circuit breaker opens because Kafka is down,
the on-call engineer needs to know within 30 seconds whether:

1. This is expected behavior (Kafka is a real incident, breaker is
   protecting the LLM budget — leave it alone)
2. The breaker has a bug (Kafka is fine, false positive — reset it)
3. The LoopAgent is in a retry storm (need to scale down the HPA)

Without runbooks, every incident becomes a re-derivation of the
architecture from source code — and that does not happen at 3 AM.

## Proposed Solution

### Step 1: Runbook directory structure

```
docs/runbooks/
├── README.md                           # index + on-call overview
├── template.md                         # runbook template
├── oncall-checklist.md                 # first 5 minutes of an incident
├── escalation.md                       # who to page, when
│
├── agents-unavailable.md               # pods crashlooping / not ready
├── high-llm-spend.md                   # cost alert fired
├── circuit-breaker-open.md             # ResiliencePlugin tripped
├── loop-agent-storm.md                 # LoopAgent runaway
├── session-db-full.md                  # Postgres out of space
├── session-db-unreachable.md           # connection errors
├── rollout-stuck.md                    # deployment not progressing
├── rate-limit-exceeded.md              # Slack webhook 429s
├── prompt-injection-detected.md        # SafetyScreenPlugin alert (AEP-013)
└── memory-service-pii-leak.md          # PII detected in redaction
```

### Step 2: Runbook template

```markdown
# Runbook: <Alert Name>

**Severity:** critical | warning | info
**Owner:** @ai-platform-team
**Auto-remediation:** none | LoopAgent | PagerDuty integration

## Symptom
What the user / alert sees.

## Diagnosis
Step-by-step commands to confirm the issue is real.

\`\`\`bash
kubectl -n orrery get pods -l app=orrery-assistant
kubectl -n orrery logs -l app=orrery-assistant --tail=200
\`\`\`

## Immediate mitigation
What to do in the first 5 minutes. Prefer reversible actions.

## Root cause investigation
Deeper checks once the bleeding has stopped.

## Permanent fix
Link to the relevant PR / ADR / AEP.

## Related
- Linked alerts
- Related runbooks
- Dashboards: https://grafana…
```

### Step 3: Wire `runbook_url` into alert rules

Extend `infra/alert_rules.yml`:

```yaml
- alert: AgentHighErrorRate
  expr: rate(tool_calls_total{status="error"}[5m]) > 0.1
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "{{ $labels.tool }} error rate > 10%"
    runbook_url: "https://github.com/BAHALLA/orrery/blob/main/docs/runbooks/high-error-rate.md"

- alert: CircuitBreakerOpen
  expr: circuit_breaker_state == 1
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "Circuit breaker open for {{ $labels.tool }}"
    runbook_url: "https://github.com/BAHALLA/orrery/blob/main/docs/runbooks/circuit-breaker-open.md"
```

### Step 4: Priority runbooks to write first

Write the 5 runbooks that cover the most likely incidents in the first
90 days of running this in production:

1. **`agents-unavailable.md`** — pods crashlooping, readiness probe failing
2. **`circuit-breaker-open.md`** — most likely false-positive pattern
3. **`high-llm-spend.md`** — ties to AEP-015 budget alerts
4. **`session-db-unreachable.md`** — Postgres connectivity (most common after an infra change)
5. **`loop-agent-storm.md`** — the scariest scenario; remediation pipeline chewing through retries

Each should have concrete `kubectl` / `curl` commands and expected
output, not abstract prose.

### Step 5: On-call overview doc

`docs/runbooks/oncall-checklist.md`:

```markdown
# On-call Checklist — First 5 Minutes

1. **Acknowledge the page.** Reply "ack" in #oncall.
2. **Identify the alert.** Click the runbook_url annotation.
3. **Confirm the symptom.** Run the diagnosis commands in the runbook.
4. **Decide: mitigate or escalate?**
   - If you know the mitigation: apply it, note the action in #incidents.
   - If not: escalate per escalation.md — do not experiment on prod.
5. **Start the incident doc.** Use the incident-doc template in the wiki.

## When to escalate immediately
- Data loss (session DB, memory service)
- Security incident (leaked credentials, PII in logs)
- LLM spend > $100 / 15 minutes
- Prompt injection alerts firing on real user traffic
```

### Step 6: Slack integration

If the platform already has a `#alerts` channel, add alert-to-runbook
preview via Slack unfurling — no new code, just a webhook config. This
is a nice-to-have and can follow after the core runbooks land.

## Affected Files

| File | Change |
|------|--------|
| `docs/runbooks/README.md` | New — index and on-call overview |
| `docs/runbooks/template.md` | New — runbook template |
| `docs/runbooks/oncall-checklist.md` | New — first-5-minutes checklist |
| `docs/runbooks/escalation.md` | New — escalation policy |
| `docs/runbooks/agents-unavailable.md` | New |
| `docs/runbooks/circuit-breaker-open.md` | New |
| `docs/runbooks/high-llm-spend.md` | New |
| `docs/runbooks/session-db-unreachable.md` | New |
| `docs/runbooks/loop-agent-storm.md` | New |
| `infra/alert_rules.yml` | Add `runbook_url` annotations to every alert |
| `mkdocs.yml` | Add runbooks section to the nav |

## Acceptance Criteria

- [x] `docs/runbooks/` directory exists with README, template, and checklist
- [x] At least 5 high-priority runbooks written with concrete diagnosis commands
- [x] Every alert in `infra/alert_rules.yml` has a `runbook_url` annotation
- [x] On-call checklist documented and linked from `docs/deployment.md`
- [x] Escalation policy documented (even if it's just "ping #ai-platform")
- [x] Runbooks published via mkdocs and cross-linked from alerts
- [ ] First dry-run: a new engineer can resolve a simulated incident using only the runbook

## Notes

- Runbooks decay. Schedule a quarterly review (pair with the price-table
  review from AEP-015).
- Prefer concrete commands over prose. "Check the Kafka connection"
  is useless; `kubectl exec -it <pod> -- python -c 'from confluent_kafka.admin import AdminClient; …'` is useful.
- Runbooks should link to dashboards, not embed screenshots — screenshots
  go out of date instantly.
- For the most dangerous incident class (LoopAgent storm), include a
  "break glass" command that scales the HPA to 0 immediately:
  `kubectl -n orrery scale deploy/orrery-assistant --replicas=0`
