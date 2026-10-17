# ADR-005: How the coordinator's specialists are chosen

**Status:** Accepted
**Date:** 2026-10-17
**Author:** Taoufiq

## Context

"Which agent handles this?" is answered at two different times, by two
different mechanisms:

1. **Which specialists exist for this deployment**, decided before any question
   is asked: by what is installed, what the operator allows, and what an admin
   switched on in the console.
2. **Which specialist answers a given request**, decided per turn by the
   coordinator model, choosing among the specialists from step 1.

Until this ADR, step 1 was only half generic. Specialists registered through
entry points, but the coordinator still carried a table of facts about the
built-in ones: a routing line per agent name, the tools each contributes to a
health sweep, the batch workflow's five checkers, and a fixed offer to save
findings in the ops journal. A third-party specialist routed on its
`description` alone, could never join a health sweep, and every new built-in
meant an edit to the coordinator. The fixed text also produced real bugs: the
sweep called tools of specialists that were switched off (#253), and the
journal was offered after it was switched off (#255).

## Decision

**A specialist declares what it contributes; the coordinator is assembled from
the declarations of the enabled specialists, and knows none by name.**

```python
specialist = SpecialistSpec(
    agent=root_agent,
    routing_hint="Kafka cluster health, topics, consumer groups, lag, ...",
    health_check=HealthCheck(
        system="Kafka", checks="...", criteria="...", tools=(get_kafka_cluster_health, ...)
    ),
    capabilities=frozenset(),  # e.g. {JOURNAL}
)
```

```toml
[project.entry-points."orrery.specialists"]
kafka = "kafka_health_agent.agent:specialist"
```

## The mechanism, end to end

```mermaid
flowchart TB
  subgraph Install["1. Installed: entry points (orrery.specialists)"]
    S1[kafka: SpecialistSpec]
    S2[k8s: SpecialistSpec]
    S3[acme-redis: SpecialistSpec<br/>third party]
  end
  Install -->|"discover_specialists()<br/>validate each spec"| Env
  Env["2. Allowed: ORRERY_SPECIALISTS / ORRERY_DISABLED_SPECIALISTS<br/>(operator, the ceiling)"] --> Cat
  Cat["3. Enabled: admin catalog<br/>(console, may only narrow + add declarative agents)"] -->|"build_chat_agent(enabled)"| Tree
  subgraph Tree["Coordinator tree (immutable, rebuilt per catalog version)"]
    R["routing section: one line per enabled routing_hint"]
    T["incident_triage_agent: union of enabled HealthChecks"]
    J["journal offer: only if a JOURNAL specialist is enabled"]
    A["AgentTool per enabled specialist"]
  end
  Tree -->|"atomic swap; in-flight turns finish on the old tree"| Gateway
  Gateway -->|"per turn: the model picks an AgentTool"| Answer
```

### Step 1: which specialists exist

| Layer | Who decides | How | Can it widen the one above? |
|---|---|---|---|
| Installed | Whoever builds the image | Python entry points, group `orrery.specialists` | n/a |
| Allowed | The operator | `ORRERY_SPECIALISTS` / `ORRERY_DISABLED_SPECIALISTS` | No: an unknown name stops startup |
| Enabled | An admin, at run time | `PUT /admin/catalog` (console Agents tab) | No: switching on an env-excluded specialist is refused |

Each layer can only narrow the one above it, so nothing pinned in a deployment
can be undone from a browser. The catalog may also *add* declarative agents,
but those only recombine tools that installed specialists already expose, each
keeping its own guard level.

### Step 2: which specialist answers

The coordinator is a chat-mode `LlmAgent`. Each enabled specialist is wrapped as
an `AgentTool` (agents-as-tools; see ADR-002/003), and the instruction's routing
section lists one line per specialist: its `routing_hint`. The model picks the
tool. This step is probabilistic by design. It is kept honest by:

- a **closed world**: the routing section, the sweep and the journal offer are
  pure functions of the enabled set, so the coordinator is never told about a
  specialist it cannot call;
- **one owner per description**: the line the model routes on is written next
  to the specialist's tools, by the people who know them;
- **evals** (`make eval`) that score routing on real questions.

A broad "is everything healthy?" goes to `incident_triage_agent`, built from the
enabled specialists' `HealthCheck`s. The batch workflow (`make run-triage`)
builds one parallel checker per `HealthCheck` from the same declarations, so the
two sweeps cannot drift apart.

## Patterns this follows

| Pattern | Where |
|---|---|
| Plugin architecture / microkernel (as pytest plugins, Flask extensions) | Entry points: a specialist is a `pip install`, not a fork |
| Self-describing components (cf. A2A Agent Cards, MCP tool descriptions) | `SpecialistSpec`: hint, health check, capabilities |
| Dependency inversion | The coordinator depends on the `orrery_core.registry` contract, never on a specialist package |
| Capability-based offers | `JOURNAL`: the coordinator offers what an enabled specialist can do, never by name |
| Layered, monotone configuration | install ⊇ env ⊇ catalog |
| Agents as tools vs workflow | LLM routing for chat; a deterministic graph for the batch sweep (ADR-003) |
| Validate by construction | The new tree is built before the catalog is saved; a failure changes nothing |
| Optimistic concurrency | Each save names its version; a stale one gets 409 (like Kubernetes `resourceVersion`) |
| Immutable snapshot + atomic swap | A new root replaces the old; turns in flight finish on the tree they started on |

## Security properties

- **Read-only sweeps, enforced at startup.** A `HealthCheck` may only list
  tools that its own agent exposes and that carry no `@confirm`/`@destructive`
  guard. Sweeps run unattended: an instruction saying "take no action" is not a
  control, so a violation stops startup instead.
- **No privilege from the browser.** The catalog only narrows the env and
  composes reviewed tools. Editing needs the admin role *and* the deployment
  flag `ORRERY_ADMIN_EDITING` (off by default). Reading needs admin.
- **Guards travel with tools.** RBAC, the confirmation gate and autonomy
  levels read the tool's own decorator wherever the tool ends up: specialist,
  declarative agent or sweep.
- **Audited.** Every catalog save emits `agent_catalog_updated` with the
  actor, version and resulting set.

## Reliability properties

- **Fail fast on bad configuration.** An unknown specialist name, an entry
  point that is neither an agent nor a spec, or an invalid health check stops
  startup with a message naming it.
- **The served tree is always one that built.** Build, then save, then swap.
  A stored catalog that no longer builds (a specialist was uninstalled) is
  logged and skipped; the current tree keeps serving.
- **No lost edits.** Concurrent admins get a conflict, not last-write-wins.
- **No torn turns.** Swapping the root never changes a turn already running.
  A thread's session survives the swap (`core/tests/test_session_continuity.py`).
- **Replicas converge.** Each polls the catalog version
  (`ORRERY_CATALOG_POLL_SECONDS`, default 15) when the store is Postgres.

## Consequences

- A new specialist, built-in or third party, is one package with one entry
  point. It routes, joins both sweeps and offers its capabilities with no edit
  to the coordinator. `agents/orrery-assistant/tests/test_admin.py` proves it
  with a "redis" specialist the coordinator has never heard of.
- Entry points that name a bare agent still work: no health check, no
  capabilities, routed by `description`.
- The routing line's quality now sits with the specialist's authors. A vague
  hint routes badly, which evals catch.
- **Known limit:** the batch workflow's `journal_writer` still calls the ops
  journal's tools directly. It runs only when a `JOURNAL` specialist is
  enabled, but a third-party journal could not replace it yet.
- Without `DATABASE_URL`, catalog edits live in memory and reset on restart.
