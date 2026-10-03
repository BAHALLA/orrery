# AEP-023: First-Class Scheduled Agent Tasks

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">in-progress</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | Medium (3-5 days) |
| **Impact** | Medium |
| **Dependencies** | AEP-011 (Postgres persistence) — strong; AEP-018 (HPA/multi-replica) — soft |

> Pattern borrowed from the Hermes agent architecture (first-class cron scheduling
> with JSON-persisted job state). Retargeted at Orrery's deterministic triage
> Workflow.

## Progress

### Increment 1 — a sweep's outcome you can alert on (shipped)

Before any scheduler exists, the one-shot `run_triage.py` that a CronJob would
wrap had to say whether it *worked*. It always exited `0`. Two outcomes were
therefore indistinguishable from a healthy sweep:

- **A sweep refused its reads.** RBAC, the autonomy level or a run budget
  ([AEP-027](aep-027-run-budgets.md)) answers instead of the tool, the checker
  reports on what it could see, and the summarizer writes "healthy" over a
  system nobody looked at. A refusal is not an all-clear.
- **A sweep with no verdict.** It raised, or the route had to infer the severity.

`orrery_assistant/batch.py` now judges each run from the session state and a
`ToolLedger` armed for the sweep. The ledger is needed because the checks run
inside graph nodes and `AgentTool` specialists whose results never reach the
runner's event stream. The run exits `0` (complete), `1` (failed / no verdict)
or `2` (incomplete: a refused read, a spent budget, or an inferred verdict),
and prints a one-line JSON summary. A remediation stopped at the confirmation
gate is reported under `awaiting_human` and does **not** count against the
sweep: that is the unattended path working as designed.

This is the run-history record Step 1 below will persist. The exit-code contract
stays the same when the scheduler replaces cron.

### Still to do

Steps 1–4 below: persisted schedules and run history, the scheduler process,
the history API and web pane, and the Helm `scheduler` Deployment.

One design note carried forward: when the scheduler grows beyond the fixed
triage Workflow into user-defined tasks that read attacker-reachable text (chat
rooms, tickets), each task should declare a **tool allow-list** enforced by a
plugin (an allow-list, not a deny-list, so a tool added later is unreachable
until a task names it). For the fixed triage Workflow the tool set *is* the
agent graph, so the allow-list would add nothing yet.

## Gap Analysis

### Current Implementation

The deterministic incident-triage Workflow exists and is designed for batch use:

- `agents/orrery-assistant/run_triage.py` runs `orrery_triage_workflow` **once**
  ("Intended for cron / CI / on-call sweeps") and exits.
- Scheduling, run history, and failure handling all live **outside** the app —
  you'd wrap the script in a host cron / Kubernetes `CronJob` and pipe logs
  somewhere yourself.

There is no first-class notion of a **schedule** or a **run history**:

- No persisted record of "run a full sweep every 15 minutes."
- No queryable history of past sweeps (verdict, severity, duration, trend).
- No in-app visibility — the web console (AEP-019) can trigger a sweep on demand
  but can't show "here's what the 02:00 sweep found."

### Why this matters now

For an SRE platform, *proactive* recurring triage is the natural mode — catch a
degradation at 02:00 before a human is paged, and show the **trend** of verdicts
over time, not just the latest one. The Workflow is already built (AEP-003/004
graph inversion); what's missing is the thin scheduling + history layer around
it. AEP-011 already gives a Postgres store to persist to, and AEP-019 gives a UI
that would immediately benefit from a sweep history pane.

### What's available

- `orrery_triage_workflow` is a ready-to-run batch root; `record_triage_verdict`
  already writes `incident_severity` + `triage_report` to state.
- `DATABASE_URL` + the persistence layer (`core/orrery_core/persistence/db.py`)
  already back sessions/memory/confirmations — a `scheduled_runs` table is a small
  addition using the same engine.
- The Kubernetes deployment can run a dedicated scheduler replica; APScheduler (or
  a DB-backed leader-elected loop) covers in-process scheduling.

## Proposed Solution

A persisted schedule + a scheduler that runs the triage Workflow on cadence and
records each run, exposed read-only through the existing HTTP/web surface.

### Step 1: Persisted schedule + run history

```sql
CREATE TABLE triage_schedules (
    id            TEXT PRIMARY KEY,
    cron          TEXT NOT NULL,            -- "*/15 * * * *"
    enabled       BOOLEAN NOT NULL DEFAULT true,
    autonomy_level TEXT NOT NULL DEFAULT 'L2',  -- sweeps stay read-only by default
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE triage_runs (
    id            TEXT PRIMARY KEY,
    schedule_id   TEXT REFERENCES triage_schedules(id),
    started_at    TIMESTAMPTZ NOT NULL,
    finished_at   TIMESTAMPTZ,
    severity      TEXT,                     -- healthy | degraded | critical
    report        TEXT,
    status        TEXT NOT NULL             -- running | ok | error
);
```

### Step 2: Scheduler process (one leader across replicas)

Add `agents/orrery-assistant/scheduler.py`:

```python
async def run_due_schedules(store: ScheduleStore):
    for sched in await store.due_now():
        run_id = await store.start_run(sched.id)
        try:
            state = await run_triage_workflow(autonomy_level=sched.autonomy_level)
            await store.finish_run(
                run_id,
                severity=state.get("incident_severity"),
                report=state.get("triage_report"),
                status="ok",
            )
        except Exception as exc:
            await store.finish_run(run_id, status="error", report=str(exc))
            logger.exception("scheduled_triage_failed", extra={"schedule_id": sched.id})
```

**Multi-replica safety** (AEP-018 territory): guard with a Postgres advisory lock
or a `SELECT … FOR UPDATE SKIP LOCKED` claim so exactly one replica runs a due
schedule — mirroring the confirmation store's atomic-claim pattern. When the
backend is in-memory, refuse to run more than one scheduler replica (same guard
the Pub/Sub worker uses).

### Step 3: Read-only API + web history pane

```
GET  /triage/schedules            → list schedules (admin)
POST /triage/schedules            → create (admin; cron + autonomy validated)
GET  /triage/runs?limit=50        → recent runs (verdict + severity + duration)
GET  /triage/runs/{id}            → full report for one run
```

Reuse the AEP-013 auth perimeter (create/modify = admin; read = operator+). The
web console (AEP-019) gains a **"Sweep history"** pane: a sparkline of severity
over time plus the latest report — turning point-in-time triage into a trend.

### Step 4: Deployment

- A dedicated `scheduler` Deployment (1 replica, or N with leader election) in the
  Helm chart, sharing the app image and `DATABASE_URL`.
- Sweeps run at **L2 (read-only)** autonomy by default — a scheduled sweep must
  never silently mutate infrastructure. Any remediation stays gated behind the
  normal L4 + confirmation flow and thus can't fire unattended.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/persistence/schedules.py` | New — `ScheduleStore` (memory + Postgres), atomic run-claim |
| `agents/orrery-assistant/scheduler.py` | New — scheduler loop over the triage Workflow |
| `core/orrery_core/serving/server.py` | Add `/triage/schedules` + `/triage/runs` routes (auth-gated) |
| `deploy/k8s/` + Helm chart | New `scheduler` Deployment; single-replica guard for memory backend |
| `web/` | New "Sweep history" pane (severity sparkline + latest report) |
| `core/tests/test_schedules.py` | New — cron matching, atomic claim, run recording, L2 default |
| `docs/agents-overview.md` / `docs/deployment.md` | Document scheduled sweeps |

## Acceptance Criteria

- [ ] `triage_schedules` + `triage_runs` persisted (Postgres; in-memory for dev)
- [ ] Scheduler runs `orrery_triage_workflow` on cron cadence and records each run
- [ ] Exactly-once execution across replicas via atomic claim (advisory lock / `SKIP LOCKED`)
- [ ] In-memory backend refuses >1 scheduler replica (mirrors Pub/Sub worker guard)
- [ ] Scheduled sweeps run at **L2 read-only** by default; no unattended mutation
- [x] Each run reports complete / failed / incomplete (exit code + JSON summary); a refused read is never reported as an all-clear
- [ ] Read-only `/triage/runs` history behind the AEP-013 auth perimeter
- [ ] Web console shows a sweep-history pane (severity trend + latest report)
- [ ] Unit tests: cron matching, atomic claim under contention, verdict recording

## Notes

- The **safety invariant is the headline**: an unattended, scheduled agent must be
  read-only by construction. Reuse `AutonomyPlugin` L2 rather than trusting the
  prompt — a scheduled sweep that could remediate on its own is a foot-gun.
- History unlocks trend analysis the point-in-time verdict can't give: "degraded
  for the last 4 sweeps" is a stronger signal than one red banner.
- Keep the scheduler dumb — it runs the *existing* Workflow. All triage logic
  stays in `orrery_triage_workflow`; this AEP adds only cadence + persistence + a
  read surface.
