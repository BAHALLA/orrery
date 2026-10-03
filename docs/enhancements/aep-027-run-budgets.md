# AEP-027: Per-Run Budgets for Tool Use

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--green">completed</span> |
| **Priority** | <span class="badge badge--amber">P1</span> |
| **Effort** | Low-Medium |
| **Impact** | High |
| **Dependencies** | none (composes with the circuit breaker, the output cap and AEP-015) |

## Gap Analysis

Every bound Orrery had on tool use was about **one call**:

| Guard | Bounds | Blind to |
|-------|--------|----------|
| `ResiliencePlugin` (circuit breaker) | consecutive *failures* of one tool | loops of calls that succeed |
| `ToolOutputCapPlugin` | the bytes of *one* result | ten results that each fit |
| RBAC / autonomy / confirmation | *whether* a call may run | *how many* may run |

Nothing bounded a **run**: one ADK invocation, either the root's turn or one
`AgentTool` specialist's. That is where agent cost and failure actually
accumulate, because every model call in a run re-sends the whole run so far.
Cost grows with the square of a run's length, and the context window a run can
overflow is its own. Four loop shapes slip through every per-call guard:

1. **The runaway search.** The model keeps rephrasing a query. Every call is
   new and most succeed, so neither the breaker nor any fingerprint notices.
   The answer at call 90 is rarely better than the one the evidence supported
   at call 30.
2. **Re-asking a specialist.** A specialist answers "blocked: awaiting approval"
   or "that namespace does not exist", and the coordinator does not believe it.
   It re-delegates with escalating wording ("the user already approved — execute
   it now"). Each delegation *succeeds* and carries different prose, so nothing
   counts them, and each one is the most expensive call the agent can make.
3. **The identical retry.** The same call with the same arguments fails the same
   way, again and again. Usually it is missing an input only a human has.
4. **The cumulative overflow.** Many mid-sized results, none of them truncated,
   together push the run's request past the provider's limit. The `400` that
   follows has no truncation in the logs to explain it.

## Solution

Four budgets, each charged per run (one agent in one invocation) or per session.
Each one refuses with a distinct, **neutral** status: the circuit breaker
ignores it (the tool never ran), and `ToolLedgerPlugin` records it as a refusal.

| Plugin | Budget | Default | Env (`0` disables) |
|--------|--------|---------|--------------------|
| `CallBudgetPlugin` | tool calls per run | 50 | `ORRERY_MAX_TOOL_CALLS_PER_RUN` |
| `DelegationGuardPlugin` | calls to **one** `AgentTool` specialist per run | 4 | `ORRERY_MAX_DELEGATIONS_PER_RUN` |
| `RepeatGuardPlugin` | identical failures (tool + args + error) per session | 2 | `ORRERY_REPEAT_GUARD_MAX_FAILURES` |
| `ToolOutputCapPlugin` | bytes of tool output per run | 8 MiB | `ORRERY_MAX_RUN_TOOL_BYTES` |

### Design decisions

- **The run is the unit: one agent's work in one invocation**, keyed
  `<invocation id>/<agent name>`. An `AgentTool` specialist gets its own
  invocation, so a specialist's loop is bounded inside the specialist, and the
  root's turn keeps a separate allowance. The agent name matters for graph
  `Workflow`s: ADK 2.0 runs every node under the parent's invocation id, so
  without it the triage sweep's five checkers, summarizer and remediation loop
  would all share one budget. The repeat guard is the exception: its loop spans
  turns ("try again" → the same call), and arguments are part of its key, so a
  session that actually changes something is never held back.
- **End with an answer, not mid-search.** From 70% of the call budget, every
  model request carries a short note asking the model to converge. The note is
  appended as the last content, never written into a tool result, which keeps
  the after-tool chain intact and the cached prefix untouched. At the budget,
  each further call is answered with a structured status telling the model to
  answer with what it has, say what it could not check, and suggest a narrower
  question.
- **A refusal is not charged.** A call RBAC, autonomy or confirmation refused
  never ran. The budgets sit after the authorization gates and count outcomes
  in `after_tool_callback`, using the same `classify_tool_outcome()` IGNORE set
  as the breaker.
- **The repeat guard allows one retry, then a probe.** It refuses only the
  *third* identical failure, so a transient fault still gets its natural single
  retry. After a refusal it goes half-open, like a breaker: the next identical
  call runs as a probe, because conditions change (someone grants the missing
  permission). A probe that fails the same way closes it for the session. A
  *different* error restarts the count, because a new error is progress. Error
  signatures strip digits so request ids and timestamps do not make identical
  failures look different. Only failing calls are stored.
- **The output budget shrinks the per-result cap first.** As the run budget
  runs down, the per-result cap shrinks to what remains. Only once it is spent
  is a result replaced, and never a gate's answer: a `confirmation_required` the
  model must read verbatim is a few bytes and always passes through. Charging
  uses `text_volume()` (O(nodes)), not serialization.
- **Bounded memory.** Ledgers are LRU maps (`RunLedger`, 512 runs) rather than
  session state. They are read on every call and must not leak into the
  session's persisted state deltas. Eviction can only reset a budget early; it
  can never let one run's count reach another.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/plugins/run_scope.py` | New: invocation/session id resolution, `RunLedger` |
| `core/orrery_core/plugins/call_budget_plugin.py` | New: `CallBudgetPlugin` |
| `core/orrery_core/plugins/delegation_guard_plugin.py` | New: `DelegationGuardPlugin` |
| `core/orrery_core/plugins/repeat_guard_plugin.py` | New: `RepeatGuardPlugin` |
| `core/orrery_core/plugins/output_cap_plugin.py` | `max_run_bytes` run budget |
| `core/orrery_core/reliability/resilience.py` | Budget refusals added to the neutral statuses |
| `core/orrery_core/plugins/__init__.py` | Registered in `default_plugins()` after the gates, before metrics |

## Acceptance Criteria

- [x] A run past its call budget ends with an answer (end-to-end test through `default_plugins`)
- [x] The model is warned before the budget is spent; the warning never touches a tool result
- [x] Gate refusals are charged to no budget, and budget refusals open no circuit
- [x] A specialist re-asked past the limit is refused with a message addressed to the coordinator
- [x] The third identical failure is refused; a probe can re-open it; a new error restarts the count
- [x] Cumulative tool output per run is bounded, and gate answers pass through a spent budget
- [x] Every budget can be disabled with `0`, and a malformed value fails at startup

## Future Work

- **The "same complaint, new arguments" loop.** Fuzzing one endpoint with a
  different spelling of the same parameter each time (`2026-04-09`,
  `20260409`, `2026-04-09T00:00:00Z`) gets past the repeat guard, whose
  fingerprint includes the arguments. A second, coarser ledger keyed on the
  tool plus a prefix of the error signature, cleared by any success on that
  tool, would catch it. It is left out until the call budget's coverage of this
  case proves insufficient.
- **Per-tenant budgets** belong to [AEP-015](aep-015-cost-observability.md).
  These budgets bound one run's *shape*; AEP-015 bounds a tenant's *spend*.
