# Runbooks

Operational procedures for **Orrery itself**. A platform that restarts pods,
rolls back deployments, alters topic configs and resets consumer-group offsets
is itself on-callable, and these are the pages you open at 03:00.

They are written for someone who did not build the system and is not going to
read the source right now. Every one carries concrete commands with the output
you should expect, not abstract prose.

## Start here

| Page | When |
|------|------|
| [On-call checklist](oncall-checklist.md) | The page just fired and you need the first five minutes |
| [Escalation](escalation.md) | You need someone else, or the incident is one of the always-escalate classes |
| [Template](template.md) | You are writing a new runbook |

## Platform runbooks

| Runbook | Alert | Severity |
|---------|-------|----------|
| [Agents unavailable](agents-unavailable.md) | `OrreryAgentDown` | critical |
| [Session DB unreachable](session-db-unreachable.md) | `OrreryDatabaseUnreachable` | critical |
| [Remediation loop storm](remediation-loop-storm.md) | `OrreryRemediationStorm` | critical |
| [Circuit breaker open](circuit-breaker-open.md) | `OrreryCircuitBreakerOpen` | warning |
| [Kafka consumer lag](kafka-consumer-lag.md) | `KafkaConsumerGroupLagHigh` | warning |
| [High tool error rate](high-tool-error-rate.md) | `OrreryHighToolErrorRate` | warning |
| [High LLM spend](high-llm-spend.md) | `OrreryHighTokenBurn` | warning |
| [Token budget store failing](token-budget-store-failing.md) | `OrreryTokenBudgetStoreFailing` | warning |
| [Prompt injection detected](prompt-injection-detected.md) | `OrreryIndirectInjectionDetected`, `OrreryDirectInjectionAttempts` | warning |
| [Unauthorized approval attempt](unauthorized-approval-attempt.md) | `OrreryUnauthorizedApprovalAttempt` | critical |
| [Unauthorized approval attempt](unauthorized-approval-attempt.md) | `OrreryUnattributableApprovals` | warning |
| [Knowledge index unavailable](knowledge-index-unavailable.md) | — (surfaces as tool errors) | warning |

Four rules in `infra/alert_rules.yml` have no dedicated page:

| Alert | Severity | Where to look |
|-------|----------|---------------|
| `TargetDown` | critical | Any scrape target is down. If it is Orrery (`job="agents"`), use [Agents unavailable](agents-unavailable.md); otherwise check that target's own pod or container. |
| `HighPrometheusScrapeLatency` | warning | A target is slow to serve `/metrics`. Usually load on that target or Prometheus itself, not Orrery. |
| `KafkaConsumerGroupLag` | warning | Monitored infrastructure, not Orrery. Same diagnosis as [Kafka consumer lag](kafka-consumer-lag.md). |
| `KafkaUnderReplicatedPartitions` | critical | Monitored infrastructure: a broker is down or lagging. Hand to the Kafka owner; Orrery's Kafka specialist can show which partitions and brokers. |

## Two things to know before you touch anything

**The agent may be right.** The most common false alarm on this platform is
mistaking *Orrery correctly reporting a real incident* for *Orrery being
broken*. A circuit breaker opening because Kafka is genuinely down is the
system working. Every runbook here starts by separating those two cases, and
that separation is usually the whole job.

**Read-only first.** Orrery's own safety model is built on the principle that
unattended processes do not mutate: scheduled sweeps run at autonomy level L2,
and guarded tools need a human approval tied to the person who asked. Extend
the same courtesy to the platform — prefer a reversible mitigation, and never
experiment on production to satisfy curiosity about a root cause. The
investigation section of each runbook exists for after the bleeding stops.

## Conventions

- `NS` is the namespace Orrery is deployed into. Set it once:
  `export NS=orrery`.
- Commands assume `kubectl` context is already pointed at the right cluster.
  Check with `kubectl config current-context` before anything destructive.
- Metric names are the real ones exported on `:9100/metrics` — `orrery_*`.
- Where a runbook says *escalate*, follow [escalation.md](escalation.md)
  rather than improvising.

## These are indexed

`make knowledge-sync` indexes this directory, so the agent can retrieve these
runbooks through `search_knowledge` (see [Knowledge Retrieval](../knowledge.md)).
Two consequences worth keeping in mind when editing:

- **Headings are retrieval units.** A passage is cited as
  "§ Immediate mitigation", so keep section names accurate and specific.
- **Stale runbooks are worse than missing ones.** Retrieval surfaces document
  age and flags anything untouched for 180 days. If you correct a procedure
  during an incident, commit the correction — an indexed lie outranks your
  memory at 03:00.
