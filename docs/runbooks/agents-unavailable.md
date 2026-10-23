# Runbook: Agents Unavailable

**Alert:** `OrreryAgentDown` · **Severity:** critical · **Owner:** @ai-platform-team
**Auto-remediation:** none

## Symptom

No replica is passing readiness. Slack and Google Chat stop answering, the web
console returns 502/503, and `up{job="agents"}` is 0. Pods may be
`CrashLoopBackOff`, or `Running` but never `Ready`.

## Is this Orrery, or is Orrery telling you the truth?

This one is unambiguous — Orrery is down. Go straight to diagnosis.

## Diagnosis

```bash
export NS=orrery
kubectl -n $NS get pods -l app.kubernetes.io/name=orrery-assistant -o wide
```

Healthy looks like `2/2 Running`, `READY 1/1`, low restart counts. The restart
count and the `STATUS` column split this into three cases:

**`CrashLoopBackOff` — the process exits at startup.** Almost always
fail-fast config validation, which is deliberate: the platform refuses to come
up half-configured rather than serve traffic with a broken security or
persistence layer.

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --previous --tail=50
```

Look for these, in rough order of frequency:

| Log line | Cause | Fix |
|----------|-------|-----|
| `JWT_SECRET is required when JWT_ALGORITHM=HS256` | Secret missing or unmounted | Restore the secret; see [deployment](../deployment.md) |
| `JWT_JWKS_URL is required when JWT_ALGORITHM=RS256` | SSO config incomplete | Set the JWKS URL |
| `DatabaseUnavailableError` | Postgres unreachable at startup | [session-db-unreachable](session-db-unreachable.md) |
| `Unknown ORRERY_KNOWLEDGE_BACKEND=…` | Typo in the knowledge backend name | Correct it or unset it — `none` disables retrieval |
| `ValidationError` from pydantic-settings | A config value has the wrong type | The message names the field |

**`Running` but not `Ready` — the process is up, its session database is not.**
`/readyz` checks only the session database (`DATABASE_URL`): never the model
provider or the systems Orrery monitors. `/healthz` only proves the process is
alive, which is why liveness is not also failing. Both are served by the front
door on its HTTP port, 8000.

```bash
kubectl -n $NS port-forward deploy/orrery-assistant 8000:8000 &
curl -s localhost:8000/readyz | jq
```

The response body names the failing check; continue with
[session-db-unreachable](session-db-unreachable.md). A broken integration
(Kafka, Kubernetes, Elasticsearch) never fails readiness: it shows up as tool
errors instead.

**`OOMKilled` or `Evicted` — resource pressure.**

```bash
kubectl -n $NS describe pod <pod> | grep -A5 'Last State'
kubectl -n $NS top pods -l app.kubernetes.io/name=orrery-assistant
```

A single 4 MiB tool result is bounded by `ToolOutputCapPlugin`, but a long
incident session accumulates transcript until compaction fires at 250k tokens.
Memory that climbs steadily over hours rather than spiking points there.

## Immediate mitigation

**Config or secret problem:** fix the value and let the rollout proceed. Do not
delete pods to "retry" — they will crashloop identically and you lose the
`--previous` logs that name the cause.

**Cause unclear and the last change was a deploy:**

```bash
kubectl -n $NS rollout history deploy/orrery-assistant
kubectl -n $NS rollout undo deploy/orrery-assistant
kubectl -n $NS rollout status deploy/orrery-assistant --timeout=120s
```

Cost of a rollback: in-flight conversations drop their turn. Sessions
themselves survive if `DATABASE_URL` is set — they live in Postgres, not in the
pod. **Pending approvals do not survive unless
`ORRERY_CONFIRMATION_BACKEND=postgres`**; with the default memory backend, every
awaiting-approval action is lost and the operator must ask again. Worth
checking before you restart anything:

```bash
kubectl -n $NS get cm,secret -o yaml | grep -i ORRERY_CONFIRMATION_BACKEND
```

**OOM:** raise the memory limit as a stopgap and note it — a limit raise that
never gets revisited is how the next OOM becomes mysterious.

## Root cause investigation

Once traffic is served again:

- Correlate the first failure with deploys and config changes:
  `kubectl -n $NS get events --sort-by=.lastTimestamp | head -40`
- If it was OOM, check whether compaction is actually running:
  `curl -s localhost:9100/metrics | grep orrery_context_compaction_total`.
  A flat counter on a long-lived deployment means `ORRERY_CONTEXT_COMPACTION`
  is disabled or the threshold is never reached.
- If readiness failed on the database, ask why it passed at deploy time. A
  readiness check that only fails hours later is usually a database
  credential expiring, not a network change.

## Permanent fix

- Config-shaped crashloops are working as designed — the gap is that the value
  was wrong, not that the platform noticed. Move the check earlier: validate
  the ConfigMap in CI rather than at pod start.
- Recurrent OOM: revisit `ORRERY_COMPACTION_TOKEN_THRESHOLD` and
  `max_tool_result_bytes` before raising limits again.
  ([AEP-020](../enhancements/aep-020-context-compaction.md))
- Lost approvals on restart: set `ORRERY_CONFIRMATION_BACKEND=postgres`. It is
  required for multi-replica anyway.

## Related

- [session-db-unreachable](session-db-unreachable.md) — the most common startup failure
- [Deployment guide](../deployment.md)
- [Escalation](escalation.md)
