# Runbook: Session Database Unreachable

**Alert:** `OrreryDatabaseUnreachable` · **Severity:** critical · **Owner:** @ai-platform-team
**Auto-remediation:** none

## Symptom

Pods fail readiness or refuse to start with `DatabaseUnavailableError`. If they
were already running, chat turns fail when the store is touched. Conversation
history, long-term memory, pending approvals and Pub/Sub idempotency claims all
sit in the same Postgres, so they degrade together.

**Which alert you get depends on when the database went away.**
`OrreryDatabaseUnreachable` keys on `orrery_tool_errors_total{error_type=…}`,
so it only fires when a *tool* raises a database error (for example
`load_memory`) on a running pod. A store that is unreachable at startup makes
the pod exit with `DatabaseUnavailableError` and crashloop, and that pages as
[`OrreryAgentDown`](agents-unavailable.md) instead.

## Why it crashes instead of degrading

This is deliberate and worth knowing before you "fix" it. Orrery **fails fast**
when `DATABASE_URL` is set but unreachable, rather than silently falling back to
in-memory storage. A pod that came up healthy while hoarding sessions locally
would split conversation history across replicas, drop pending approvals on
restart, and lose an entire incident's memory with no error anywhere.

`ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1` opts into the old behaviour. It exists for
local development. **Do not set it in production to clear this alert** — it
converts a loud outage into a silent correctness bug, and the symptoms surface
days later as "the agent forgot".

## Diagnosis

```bash
export NS=orrery
kubectl -n $NS logs -l app.kubernetes.io/name=orrery-assistant --tail=50 \
  | jq -r 'select(.message | test("Database|postgres|asyncpg"; "i")) | .message'
```

The DSN is masked in logs (`mask_dsn`), so you will see the host without the
password.

**Is Postgres up?** The Helm chart does not ship a database, so the
`postgresql` names below are examples: adapt them to how your Postgres is
deployed (or run the `psql` checks from any pod that can reach it).

```bash
kubectl -n $NS get pods -l app.kubernetes.io/name=postgresql
kubectl -n $NS run pgcheck --rm -it --restart=Never --image=postgres:16-alpine -- \
  psql "$DATABASE_URL" -c 'select 1'
```

Work through the four usual causes in this order — they are ordered by how
often they are the answer:

| Symptom from the check above | Cause |
|---|---|
| `password authentication failed` | Rotated credential not propagated to the pod |
| `could not translate host name` | Service name or namespace changed |
| `Connection refused` | Postgres down, or a NetworkPolicy blocking egress |
| `too many clients already` | Connection pool exhausted — see below |

**Connection exhaustion** deserves its own check, because it looks like an
outage but the database is fine:

```bash
kubectl -n $NS exec deploy/postgresql -- \
  psql -U agents -d agents -c \
  "select state, count(*) from pg_stat_activity group by state;"
```

Many `idle in transaction` rows point at the application; a high total against
`max_connections` points at replica count times pool size.

**NetworkPolicy** is the one people forget after an infra change:

```bash
kubectl -n $NS get networkpolicy
```

## Immediate mitigation

**Credential rotation:** update the secret, then restart to pick it up.

```bash
kubectl -n $NS rollout restart deploy/orrery-assistant
kubectl -n $NS rollout status deploy/orrery-assistant --timeout=120s
```

**Postgres genuinely down:** this is a hard dependency. Restore it — there is
no correct Orrery-side mitigation. Resist the fallback env var.

**Connection exhaustion:** scale Orrery down to reduce pool pressure while you
investigate. Fewer replicas serving is better than none.

```bash
kubectl -n $NS scale deploy/orrery-assistant --replicas=1
```

**Disk full** (`could not extend file`): free space before anything else. The
memory table grows without bound by design — `MemoryPlugin` saves every session
of four or more events, append-only.

```bash
kubectl -n $NS exec deploy/postgresql -- \
  psql -U agents -d agents -c \
  "select pg_size_pretty(pg_total_relation_size('orrery_memory_events'));"
```

Trimming the oldest memory rows is the safest space to reclaim — recall is
recency-biased anyway, so old rows contribute least:

```sql
-- Check first, then delete. Sessions and audit are NOT safe to trim casually.
delete from orrery_memory_events where ts < extract(epoch from now() - interval '90 days');
```

## Root cause investigation

- Was this a credential rotation without a rollout? That is the most common
  cause, and the fix is automation, not a runbook.
- Check growth rate before assuming disk was a one-off:
  `pg_total_relation_size` on `orrery_memory_events` week over week.
- If connections exhausted, compute the real ceiling: replicas × pool size ×
  (sessions + memory + confirmations, which share an engine per process) against
  Postgres `max_connections`.

## Permanent fix

- Rotate credentials through a mechanism that triggers a rollout.
- Add a retention policy for `orrery_memory_events`. The table is append-only
  by design and nothing prunes it today.
- Size `max_connections` against maximum replica count, including the HPA
  ceiling and the Pub/Sub worker.

## Related

- [agents-unavailable](agents-unavailable.md) — this alert usually fires alongside it
- [Cross-session memory](../memory.md) — what lives in `orrery_memory_events`
- [Deployment guide](../deployment.md)
