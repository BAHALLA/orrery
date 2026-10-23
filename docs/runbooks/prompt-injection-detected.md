# Runbook: Prompt Injection Detected

**Alerts:** `OrreryIndirectInjectionDetected`, `OrreryDirectInjectionAttempts`
**Severity:** warning · **Owner:** @ai-platform-team
**Auto-remediation:** yes — the request is blocked, or the span is neutralized in place

## Symptom

`SafetyScreenPlugin` engaged. Either a user message was refused before it cost
a token, or a tool result came back with a span replaced by the filter marker.

**Which alert fired tells you which, and they mean different things:**

| Alert | Direction | Reading |
|---|---|---|
| `OrreryIndirectInjectionDetected` | Tool result neutralized | Attacker-reachable text is sitting in the **monitored infrastructure**. A finding about that system. |
| `OrreryDirectInjectionAttempts` | User messages blocked (>3 in 15m) | Someone is **probing the agent**. A conversation to have with that person. |

Do not sum them — different owners, different responses.

## What already happened — read this before acting

The screen handles the two directions **differently**, and knowing which one
fired tells you how worried to be:

- **Direct (a user message).** Blocked in `before_run_callback` — the only hook
  whose non-None return halts the runner. It reached no tool and cost no
  tokens. **The defence worked completely.**
- **Indirect (a tool result).** The matched span is *neutralized in place*,
  not dropped. A pod annotation, log line, Kubernetes event, Elasticsearch
  document — or now a retrieved runbook — is attacker-reachable text arriving
  with a tool result's authority. It is also the evidence the agent was asked
  to read, so rejecting the whole payload would break the diagnosis.

So the alert is a *report that a control engaged*, not a report that something
got through. The question is never "did it work" but **"why is that text in my
infrastructure?"**

## Is this Orrery, or is Orrery telling you the truth?

- **A false positive.** Legitimate text that reads like an instruction — a
  runbook that literally says "ignore previous instructions", a postmortem
  quoting an attack. Annoying, low risk.
- **Injected content in the monitored system.** Someone put instruction-shaped
  text where the agent would read it. **This is a finding about your
  infrastructure**, not about Orrery, and it is the case that matters.
- **A user probing the agent.** Direct injection from a verified account. A
  conversation to have with that person.

## Diagnosis

Start with the counter — `source` names the tool whose results carried the
text, which is the fastest route to where it lives:

```bash
export NS=orrery
kubectl -n $NS port-forward svc/orrery-assistant 9100:9100 &
curl -s localhost:9100/metrics | grep '^orrery_safety_screen_total'
```

Then read the detections themselves. The counter says *how much*; the log says
*what*:

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=2000 \
  | jq -r 'select(.logger=="orrery.safety") | "\(.timestamp) \(.message)"' | tail -30
```

**Which direction?** A blocked run logs `blocked potential prompt injection` with
the message text; a neutralized result names the tool. That single fact routes the rest of this runbook.

**For an indirect hit, find the source.** The tool that returned it tells you
where to look — a pod annotation, a log line, an indexed document:

```bash
# Example: instruction-shaped text in pod annotations
kubectl get pods -A -o json \
  | jq -r '.items[] | select(.metadata.annotations // {} | tostring
           | test("ignore (all )?previous|system prompt|you are now"; "i"))
           | "\(.metadata.namespace)/\(.metadata.name)"'
```

If the source is the knowledge corpus, check who can write to it — a Confluence
space with broad edit rights is a supply chain into the agent's context.

**Is it one caller?**

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=2000 \
  | jq -r 'select(.event=="tool_attempt") | "\(.timestamp) \(.user_id) \(.session_id) \(.tool)"'
```

The `orrery.safety` lines carry no caller, so match their timestamps against the
`tool_attempt` audit lines above (which carry `user_id` and `session_id`).

## Immediate mitigation

**In almost every case: nothing.** The control engaged. Removing or loosening
it is the wrong response to it working.

**Never disable the screen to clear the alert.** `ORRERY_SAFETY_SCREEN=false`
turns off *both* directions — a one-line change that removes the platform's
only defence against indirect injection, on a system holding `@destructive`
tools.

Do act if:

- **Injected content is genuinely present in infrastructure** — remove it, and
  treat it as a security incident: [escalate](escalation.md). Someone had write
  access to something the agent reads.
- **A user is probing repeatedly** — their role is in the audit trail; reduce
  it if needed.
- **False positives are frequent enough to be noise** — that is a pattern-tuning
  task, not an incident. Note the text and raise it with the platform team.

## Root cause investigation

- **Trace the write path.** For an indirect hit: who can set that annotation,
  write that log line, index that document? That access list is the real
  finding.
- **Check what the agent did next.** Neutralization keeps the surrounding
  payload, so confirm the diagnosis that followed was not distorted by it.
- **Check the guarded-action trail.** Injection aims at making the agent act.
  Confirm nothing guarded ran in that invocation — if something did, this is a
  full security incident.

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=2000 \
  | jq -r 'select(.message | test("tool_attempt")) | select(.tool | test("scale|restart|delete|rollback|reset|alter"))'
```

## Permanent fix

- Injected content found: fix the write path. The agent reading it is the
  symptom; broad write access is the cause.
- Repeated false positives: tune the patterns, with a test case per change.
- If the source was the knowledge corpus, tighten which sources are indexed —
  [AEP-025](../enhancements/aep-025-knowledge-retrieval.md) requires indexing
  only what every viewer may read, and that rule exists for this.

## Related

- [Guardrails & RBAC](../guardrails.md)
- [Knowledge retrieval](../knowledge.md) — retrieved documents are screened on the same path
- [Escalation](escalation.md)
