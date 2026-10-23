# On-Call Checklist — First Five Minutes

**Severity:** n/a · **Owner:** @ai-platform-team

## Before anything else

```bash
kubectl config current-context     # are you where you think you are?
export NS=orrery
```

Getting this wrong is the most expensive mistake available in the first minute.

## The five minutes

**1. Acknowledge.** Reply `ack` in `#oncall` so nobody else starts a parallel
investigation. Say which alert you took.

**2. Open the runbook.** Every alert carries a `runbook_url` annotation. Click
it. If an alert has no runbook, that is a bug — file it after the incident and
see [template.md](template.md).

**3. Decide which system is broken.** This platform monitors infrastructure, so
almost every alert has two readings:

- *Orrery is reporting a real incident.* Kafka is down, the cluster is
  degraded, a deployment is stuck. **Orrery is working.** Hand off to whoever
  owns that system and stop touching Orrery.
- *Orrery itself is broken.* It cannot reach its database, its pods are
  crashlooping, it is burning tokens in a loop.

Separating these is usually the whole job. Each runbook's *Diagnosis* section
does exactly this first.

**4. Confirm the symptom.** Run the diagnosis commands. Do not skip to
mitigation because the alert name sounded obvious — alert names describe
symptoms, not causes.

**5. Mitigate or escalate.**

- Know the mitigation? Apply it, then post what you did in `#incidents`
  — the action, the time, and the expected effect.
- Don't? [Escalate](escalation.md). Do not experiment on production.

## Escalate immediately, without diagnosing first

- **Data loss** — the session or memory store has lost or corrupted data.
- **Security** — leaked credentials, PII in logs, or a prompt-injection alert
  firing on real user traffic.
- **Unapproved destructive action** — any evidence that a `@confirm` or
  `@destructive` tool ran without a human approval. This is the platform's
  central safety promise; treat a breach as a security incident.
- **Spend** — LLM cost above roughly $100 in 15 minutes.

## The three commands worth memorising

```bash
# Is Orrery up?
kubectl -n $NS get pods -l app.kubernetes.io/name=orrery-assistant

# What has it been doing? (structured JSON — jq is your friend)
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=200 \
  | jq -r 'select(.level=="ERROR") | "\(.timestamp) \(.logger) \(.message)"'

# What does it think of itself?
kubectl -n $NS port-forward svc/orrery-assistant 9100:9100 &
curl -s localhost:9100/metrics | grep -E '^orrery_(circuit_breaker_state|tool_errors_total)'
```

## Writing it down

Start an incident doc as soon as you have confirmed a real incident, not after
it is resolved. Record:

- What fired, and when.
- Which of the two readings above turned out to be true.
- Every command you ran that **changed** something.
- What you expected it to do, and what it actually did.

That last pair is what makes a postmortem worth reading, and — once
[AEP-026](../enhancements/aep-026-experience-capture-rex.md) lands — it is
what the platform mines to propose new runbooks.

## Related

- [Escalation policy](escalation.md)
- [Runbook index](README.md)
- [Deployment guide](../deployment.md)
- Grafana: *Orrery — Agent Observability* (`make up PROFILES=tracing`)
