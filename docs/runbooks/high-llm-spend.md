# Runbook: High LLM Spend

**Alert:** `OrreryHighTokenBurn` · **Severity:** warning · **Owner:** @ai-platform-team
**Auto-remediation:** none

## Symptom

`orrery_llm_tokens_total` climbing far above baseline. Cost alerts fire, or
provider quota errors start appearing in tool results.

## Is this Orrery, or is Orrery telling you the truth?

A major incident *should* cost more — five specialists sweeping a degraded
cluster is expensive by design, and that spend is the platform working. The
question is whether the spend is **proportionate to real work**.

Three shapes, distinguishable by the token direction split:

| Shape | Signature | Reading |
|---|---|---|
| Genuine incident load | Input and output both up; tool calls up proportionally | Working as intended |
| Loop | Output up, same tools repeating | [remediation-loop-storm](remediation-loop-storm.md) |
| Context bloat | **Input** up sharply, output flat, tool calls flat | Transcript or tool results growing |

The third is the quiet one: cost grows while the platform appears to be doing
the same amount of work.

## Diagnosis

```bash
export NS=orrery
kubectl -n $NS port-forward svc/orrery-assistant 9100:9100 &

# Direction split — this is the diagnostic
curl -s localhost:9100/metrics | grep '^orrery_llm_tokens_total'

# Is work actually up, or just cost?
curl -s localhost:9100/metrics | grep '^orrery_tool_calls_total' | sort -t' ' -k2 -rn | head

# Cache and compaction — both reduce input tokens
curl -s localhost:9100/metrics | grep -E '^orrery_(context_cache_events_total|context_compaction_total)'
```

**Input tokens up, output flat** — work through these:

- **Cache misses.** A low hit ratio on `orrery_context_cache_events_total`
  means the cached prefix is being invalidated every turn. Context caching only
  applies to Gemini models; on another provider the counter stays flat and this
  is not your cause.
- **Compaction not firing.** A flat `orrery_context_compaction_total` on a
  deployment with long sessions means the transcript is growing unbounded.
  Check `ORRERY_CONTEXT_COMPACTION` is not `false` and that
  `ORRERY_COMPACTION_TOKEN_THRESHOLD` (250k) is actually reachable.
- **Fat tool results.** `ToolOutputCapPlugin` bounds each result at 4 MiB, but
  a result just under the cap re-enters the prompt on every subsequent turn. A
  wide Elasticsearch query or an unfiltered `logs` call does this.

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=2000 \
  | jq -r 'select(.message | test("truncat"; "i"))' | tail -20
```

**Output tokens up, same tools repeating** — a loop. Go to
[remediation-loop-storm](remediation-loop-storm.md).

**Everything up together** — check whether it is one caller:

```bash
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=2000 \
  | jq -r 'select(.message | test("tool_attempt")) | .user_id' | sort | uniq -c | sort -rn | head
```

## Immediate mitigation

**Emergency stop, in increasing order of disruption.**

1. **Suspend scheduled sweeps.** Free, and often sufficient — a triage sweep is
   five agents' worth of tokens per run.

```bash
kubectl -n $NS patch cronjob orrery-triage -p '{"spec":{"suspend":true}}'
```

2. **Tighten the chat rate limit.** Bounds per-caller spend without taking the
   platform away.

```bash
kubectl -n $NS set env deploy/orrery-assistant ORRERY_CHAT_RATE_LIMIT=5/minute
```

3. **Drop to a cheaper model.** Degrades reasoning quality; acceptable for a
   few hours.

```bash
kubectl -n $NS set env deploy/orrery-assistant MODEL_NAME=gemini-3.1-flash-lite
```

4. **Read-only.** Stops remediation loops outright while keeping diagnosis.

```bash
kubectl -n $NS set env deploy/orrery-assistant ORRERY_AUTONOMY_LEVEL=L2
```

Above roughly **$100 in 15 minutes**, apply step 1 and
[escalate](escalation.md) rather than working down the list.

## Root cause investigation

- Compare tokens per tool call over time. Rising tokens with flat calls is the
  context-bloat signature, and it usually traces to one chatty tool.
- Check session length distribution. Compaction is lossy for the model but
  keeps the record; if sessions never get long enough to compact yet still cost
  a lot, the problem is per-turn payload, not history.
- If a single caller dominates, that is a usage-pattern conversation, not an
  engineering fix.

## Permanent fix

- Narrow the offending tool's default result size at the source — a smaller
  default beats a truncation note the model has to work around.
- [AEP-015](../enhancements/aep-015-cost-observability.md) adds per-tenant
  budgets and is the real answer to "who is spending this".
- If cache hit ratio is chronically low, the instruction or tool set is
  changing every turn; stabilise the prefix.

## Related

- [remediation-loop-storm](remediation-loop-storm.md)
- [Metrics reference](../metrics.md)
- [Tool results & the output cap](../tool-results.md)
