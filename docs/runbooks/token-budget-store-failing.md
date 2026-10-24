# Runbook: Token Budget Store Failing

**Alert:** `OrreryTokenBudgetStoreFailing` · **Severity:** warning · **Owner:** @ai-platform-team
**Auto-remediation:** none

## Symptom

`orrery_token_budget_store_errors_total` is rising. Users notice nothing:
the daily token budgets fail open, so while the store is unreachable every
model call goes ahead **without a budget check** and is not counted.

## Is this Orrery, or is Orrery telling you the truth?

The budgets share their store with the HTTP rate limits
(`ORRERY_RATE_LIMIT_STORAGE_URI`, usually Redis). If the rate limits are also
failing, the store is down and Orrery is reporting it correctly. If only the
budget calls fail, look at the timeout: each store call gets 2 s, so a slow
but reachable store also counts here.

## Diagnosis

```bash
export NS=orrery
kubectl -n $NS logs deploy/orrery-assistant | grep token_budget_store_error | tail
kubectl -n $NS get pods | grep -i redis
redis-cli -u "$ORRERY_RATE_LIMIT_STORAGE_URI" ping   # expect PONG, well under 2 s
```

## Immediate mitigation

Restore the store. While it is down, spending is bounded only by the rate
limits and the run budgets. If the bill matters more than availability,
scale the assistant down until the store is back: that stops all model
calls, budgeted or not.

## Root cause investigation

Check the store's memory, eviction policy and network path from the
assistant's pods. Budget counters expire after two days on their own, so
they are small; a store under pressure is usually full of something else.

## Permanent fix

None in Orrery: failing open is deliberate. A budget is a cost control, and
failing closed would make the store a dependency of every conversation.

## Related

- `OrreryHighTokenBurn` ([High LLM spend](high-llm-spend.md)), when spend rises while budgets are off
