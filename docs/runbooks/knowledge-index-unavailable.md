# Runbook: Knowledge Index Unavailable

**Alert:** none — surfaces as `KnowledgeBackendError` in tool results
**Severity:** warning · **Owner:** @ai-platform-team · **Auto-remediation:** none

## Symptom

`search_knowledge` returns
`status: error, error_type: KnowledgeBackendError`. The agent still diagnoses
from live signals, but without runbooks or past postmortems — so it improvises
procedures the team has already written down.

## Is this Orrery, or is Orrery telling you the truth?

Neither, usually. The knowledge layer is an **augmentation, not a dependency**:
misconfiguration fails fast at startup, but a backend that goes away at query
time deliberately degrades to a tool error instead of taking the platform down.
The platform diagnosed incidents without retrieval before it existed.

So this is rarely urgent. It is worth fixing promptly because a silently empty
corpus makes the agent *less* useful in exactly the moments it matters most.

**Important:** "no results" and "backend unreachable" are deliberately distinct
results. If the tool returns `status: success` with an empty `results` list,
the index is reachable and genuinely has nothing — that is a corpus gap, not an
outage. Skip to *Corpus is empty* below.

## Diagnosis

```bash
export NS=orrery
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=500 \
  | jq -r 'select(.logger | test("knowledge")) | "\(.timestamp) \(.message)"' | tail -20
```

**Is the backend configured at all?**

```bash
kubectl -n $NS get cm -o yaml | grep -E 'ORRERY_KNOWLEDGE_BACKEND|KNOWLEDGE_ES_'
```

`none` (the default) means retrieval was never enabled — the tool is not
attached and cannot be erroring. If you expected it enabled, this is the
finding.

**Is the index cluster reachable, and does the index exist?**

```bash
kubectl -n $NS run escheck --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s http://elasticsearch:9200/_cluster/health

kubectl -n $NS run escheck --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s http://elasticsearch:9200/orrery-knowledge/_count
```

A `404 index_not_found_exception` means sync has never run against this
cluster. `{"count": 0}` means it ran but indexed nothing.

Remember `KNOWLEDGE_ES_*` is **not** `ELASTICSEARCH_*` — the Elasticsearch
*agent* monitors somebody's production cluster; the knowledge layer indexes our
own corpus. Pointing one at the other is a plausible mistake worth ruling out.

## Immediate mitigation

**Cluster down:** the agent already degrades correctly. Restore Elasticsearch;
no Orrery-side action.

**Index missing or empty:** re-run sync.

```bash
make knowledge-sync
```

Expected output names what it did:

```
indexed 12 document(s) as 148 chunk(s); skipped 0 unchanged; pruned 0 stale chunk(s); removed 0 deleted document(s)
```

**Wrong cluster configured:** correct `KNOWLEDGE_ES_URL` and restart. The
config is read at process start.

**Corpus is empty (`status: success`, no results):** nothing is broken —
nothing is written down. Note the gap and write the runbook afterwards. That is
the intended workflow, not a failure.

## Root cause investigation

- **Did a sync silently half-fail?** `make knowledge-sync` exits non-zero on
  any per-document error, precisely so CI cannot go green having dropped half
  the runbooks. Check the CI job, not just the index count.
- **Did pruning remove more than expected?** Sync skips deletion-pruning
  entirely when a run had errors, but a *successful* run against a narrower set
  of roots legitimately prunes everything outside them. Compare the roots the
  job used against the defaults.
- **Are documents stale rather than absent?** Retrieval flags anything
  untouched for 180 days. A corpus that returns only stale hits needs authoring
  attention, not operational attention.

```bash
kubectl -n $NS run escheck --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s 'http://elasticsearch:9200/orrery-knowledge/_search?size=0' \
  -H 'Content-Type: application/json' \
  -d '{"aggs":{"by_uri":{"terms":{"field":"uri","size":100}}}}'
```

## Permanent fix

- Run `make knowledge-sync` from CI on merge to `main` so the index tracks the
  repository rather than drifting from it.
- Alert on index document count dropping to zero — an empty index currently
  looks identical to a corpus nobody has written yet.
- If the cluster is shared with the monitored Elasticsearch, give the knowledge
  index its own cluster. A production incident should not also take away the
  runbooks for it.

## Related

- [Knowledge retrieval](../knowledge.md)
- [AEP-025](../enhancements/aep-025-knowledge-retrieval.md)
- [high-tool-error-rate](high-tool-error-rate.md)
