# Runbook: Remediation Loop Storm

**Alert:** `OrreryRemediationStorm` · **Severity:** critical · **Owner:** @ai-platform-team
**Auto-remediation:** none — this *is* the auto-remediation misbehaving

## Symptom

The triage Workflow's remediation subgraph is cycling: repeated
`remediation_actor` → `remediation_verifier` tool calls, climbing token spend,
and — the part that matters — **repeated mutating actions against production**.
Typically noticed as a deployment being scaled or restarted several times in a
few minutes.

This is the scariest scenario the platform has, because it is the one path
that runs with **no human in the conversation**.

## Is this Orrery, or is Orrery telling you the truth?

Both can be true, and the distinction changes what you do:

- **A real incident the remediation cannot fix.** The loop is behaving as
  designed — act, verify, fail, retry — and hitting its cap. The underlying
  fault is real and needs a human.
- **The loop itself is wrong.** The verifier cannot observe the effect of the
  action, so it never marks the incident resolved and the actor keeps acting on
  a system that was already fine.

The second is worse and less obvious. Check whether the *system being
remediated* was ever actually unhealthy before the first action.

## What is supposed to bound this

Three mechanisms, and knowing which one failed tells you the fix:

1. **Iteration cap.** `verify_route` enforces `MAX_REMEDIATION_ITERATIONS = 3`
   act→verify cycles via a state counter. A *single* sweep cannot exceed three
   actions.
2. **Confirmation gate.** `remediation_actor` wires
   `before_tool_callback=require_confirmation()` like every other tool-calling
   agent. This is load-bearing here specifically: `run_triage.py` pins the
   session to `operator`, which RBAC lets past `@confirm` tools such as
   `scale_deployment`.
3. **Autonomy level.** A scheduled sweep should run at **L2 (read-only)**.

So a genuine storm means one of: the sweep is being *re-triggered* repeatedly
(cap works per-run, not across runs), the confirmation gate is not wired, or
autonomy is set above L2 for an unattended process.

## Diagnosis

```bash
export NS=orrery

# What has the actor actually done? Audit records every attempt.
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=1000 \
  | jq -r 'select(.message | test("tool_attempt")) | "\(.timestamp) \(.agent) \(.tool)"' \
  | grep remediation
```

**How many distinct sweeps?** Count invocations, not tool calls. Three actions
in one sweep is the cap working; thirty means the sweep is being re-run.

```bash
kubectl -n $NS get cronjob,job -o wide | grep -i triage
```

**Is anything being blocked or paused?** These strings are the gate working:

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=1000 \
  | jq -r 'select(.message | test("BLOCKED|AWAITING_CONFIRMATION"))' | tail -20
```

`AWAITING_CONFIRMATION` means the action paused for approval — correct
behaviour for an unattended run, and the sweep should end there rather than
proceed. `BLOCKED` means autonomy refused it outright.

**What autonomy level is the sweep running at?**

```bash
kubectl -n $NS get cm -o yaml | grep -i ORRERY_AUTONOMY_LEVEL
```

Anything other than `L2` for a scheduled sweep is the finding.

## Immediate mitigation

**Stop the sweeps first, ask why second.** The Helm chart does not ship a
triage CronJob; `orrery-triage` below is an example name, so use whatever
schedules `run_triage.py` in your deployment.

```bash
kubectl -n $NS patch cronjob orrery-triage -p '{"spec":{"suspend":true}}'
kubectl -n $NS delete job -l job-name --field-selector status.successful!=1
```

If triage runs inside the main deployment rather than a CronJob, drop it to
read-only rather than removing the capability:

```bash
kubectl -n $NS set env deploy/orrery-assistant ORRERY_AUTONOMY_LEVEL=L2
kubectl -n $NS rollout status deploy/orrery-assistant --timeout=120s
```

L2 is fail-closed: only unguarded read tools and an explicit whitelist run. It
stops mutation without stopping diagnosis, so the platform keeps being useful
while you work.

**Then assess the damage.** List every mutating action that actually executed,
and reverse the ones that were wrong:

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=2000 \
  | jq -r 'select(.message | test("tool_attempt")) | select(.tool | test("scale|restart|rollback|delete|reset|alter"))'
```

## Root cause investigation

- **Was the verifier able to observe the fix?** A verifier querying a metric
  with a slow scrape interval will always see the pre-action value and never
  resolve. That produces exactly three actions per sweep, every sweep, forever
  — the cap holds but the loop is useless.
- **Was the incident real?** Check whether `record_triage_verdict` ran. If it
  did not, `triage_route` infers severity from per-system reports and flags
  `triage_verdict_missing` — a fail-safe that can route to remediation on
  thin evidence.
- **If any `@confirm` tool executed with no matching approval**, stop and treat
  it as a security incident: [escalate](escalation.md) immediately. That is a
  breach of the platform's central safety promise, not a tuning problem.

## Permanent fix

- Pin scheduled sweeps to `ORRERY_AUTONOMY_LEVEL=L2`. An unattended agent
  should be read-only *by construction*, not by prompt.
- If the verifier cannot observe its own effect, that is a verifier bug — give
  it a signal with a latency shorter than the loop, or lengthen the wait
  between act and verify.
- Confirm `agents/orrery-assistant/tests/test_confirmation_wiring.py` still
  covers the actor. It walks both roots precisely because this gap recurred
  here once already. Extend the walker, never the exception list.

## Related

- [ADR-003: graph workflow inversion](../adr/003-graph-workflow-inversion.md)
- [Guardrails & RBAC](../guardrails.md)
- [high-llm-spend](high-llm-spend.md) — a storm shows up there too
- [Escalation](escalation.md)
