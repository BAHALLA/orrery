# Runbook: Circuit Breaker Open

**Alert:** `OrreryCircuitBreakerOpen` · **Severity:** warning · **Owner:** @ai-platform-team
**Auto-remediation:** self-healing — the breaker half-opens after 60s

## Symptom

`orrery_circuit_breaker_state{tool="…"} == 1`. Calls to that one tool are
refused immediately without reaching the backend. The agent reports the tool as
unavailable; every other tool keeps working.

## Is this Orrery, or is Orrery telling you the truth?

**Start here — this is the alert most often misread on the platform.**

`ResiliencePlugin` opens a breaker after **5 consecutive failures** for a
single tool. Five consecutive failures of `get_cluster_health` almost always
means the cluster is genuinely unreachable. The breaker is then doing its job:
protecting the LLM budget and the backend from an agent retrying into a
brownout.

Three readings, in order of likelihood:

1. **The monitored system is down.** Breaker is correct. Leave it alone, hand
   off to that system's owner. *This is the common case.*
2. **The monitored system is fine and the breaker is stuck.** Credentials
   expired, a NetworkPolicy changed, DNS moved — the failures were real but the
   cause is Orrery-side configuration.
3. **The breaker is a false positive.** Rare. Usually a slow backend where the
   tool's own timeout fires before the backend answers.

## Diagnosis

```bash
export NS=orrery
kubectl -n $NS port-forward svc/orrery-assistant 9100:9100 &

# Which tools are open? 0=closed 1=open 2=half-open
curl -s localhost:9100/metrics | grep '^orrery_circuit_breaker_state' | grep -v ' 0$'

# Which tools have been failing? (raised or returned errors)
curl -s localhost:9100/metrics | grep '^orrery_tool_calls_total' | grep 'status="error"' \
  | sort -t' ' -k2 -rn | head

# What kind of failure? (only tools that raised an exception)
curl -s localhost:9100/metrics | grep '^orrery_tool_errors_total' | sort -t' ' -k2 -rn | head
```

Most tools return `{"status": "error", ...}` instead of raising, and those
calls have no `error_type`: read the reason from the `tool_call` audit lines
(`kubectl logs ... | jq -c 'select(.status=="error") | {timestamp, tool, response}'`).
When the reason, or the `error_type` label, is available, it decides which
reading applies:

| `error_type` | Reading | Next step |
|---|---|---|
| `ConnectionError`, `TimeoutError` | Backend down or unreachable | Check the backend directly |
| `AuthenticationError`, `403`, `401` | Credential expired | Orrery-side config |
| `NotFound`, `404` | Resource renamed or deleted | Usually a real change, not a fault |
| `ValidationError` | The model is calling the tool wrong | Not a breaker problem — see below |

**Confirm from outside Orrery.** Do not trust the agent's own view here:

```bash
# Example: Elasticsearch
kubectl -n $NS run escheck --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s http://elasticsearch:9200/_cluster/health

# Example: Kubernetes tools — check the service account still has rights
kubectl -n $NS auth can-i list pods --as=system:serviceaccount:$NS:orrery-assistant
```

If the backend answers fine from a neighbouring pod, you are in reading 2 or 3.

## Immediate mitigation

**Reading 1 (backend down):** nothing to do on Orrery. The breaker half-opens
after 60 seconds and closes itself on the first success. Do not restart pods to
"clear" it — you would lose the protection and re-enter the brownout.

**Reading 2 (config drift):** fix the credential or policy, then let recovery
happen naturally. The breaker probes on its own; a restart is only needed if
the fix requires re-reading a mounted secret.

```bash
kubectl -n $NS rollout restart deploy/orrery-assistant
```

Cost: in-flight turns drop, and pending approvals are lost unless
`ORRERY_CONFIRMATION_BACKEND=postgres`.

**Reading 3 (false positive on a slow backend):** raise the tool's timeout
rather than the breaker threshold. A higher threshold just means five slow
failures instead of five fast ones — it delays the breaker without fixing
anything.

## Root cause investigation

- **Is one tool open, or several?** Several tools against the same backend is a
  backend or network fault. Several tools across *different* backends points at
  Orrery's egress — DNS, NetworkPolicy, or the node.
- **Does it reopen on a cycle?** A breaker that opens, closes, and reopens
  every few minutes is a flapping backend. That is worth an alert on the
  backend, not on Orrery.
- **`ValidationError` in the mix** means the model is calling the tool with bad
  arguments and the breaker is counting model error as backend failure. That is
  a prompt or schema problem; the breaker is a bystander.

## Permanent fix

- Credential expiry: rotate through something that triggers a rollout, and
  alert on approaching expiry rather than on the breaker downstream of it.
- Persistent flapping: tune `circuit_breaker_threshold` /
  `circuit_breaker_timeout` in `default_plugins()` for that deployment's
  latency profile — but only after confirming the backend is genuinely healthy.
- Repeated `ValidationError`: fix the tool's description or its input schema so
  the model stops mis-calling it.

## Related

- [high-tool-error-rate](high-tool-error-rate.md) — usually fires first
- [agents-unavailable](agents-unavailable.md) — if every tool is failing, not one
- [Metrics reference](../metrics.md)
