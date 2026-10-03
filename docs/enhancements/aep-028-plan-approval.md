# AEP-028: Plan-Level Approval for Multi-Step Changes

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | Medium (3-5 days) |
| **Impact** | Medium-High |
| **Dependencies** | AEP-001 (confirmation), AEP-013 (requester-verified approval), AEP-024 (approval audit events) |

## Gap Analysis

### Current Implementation

Every `@confirm`/`@destructive` call is approved **one at a time**. The gate
hashes the tool name and arguments, the human approves that exact call, and the
approval is valid for that call within a short window
(`core/orrery_core/security/confirmation_store.py`).

That is the right default for one action. It degrades badly for the work an
SRE actually asks for, which is a *sequence*:

- "Roll back `payments-api`, then scale it to 6, then restart the worker": three
  cards, three round trips, three chances for the window to close mid-sequence.
- The closed-loop remediation (`remediation_actor ⇄ remediation_verifier`) can
  retry an action with *different* arguments after a failed verification. Each
  retry is a new hash, a new card, and a new interruption.
- Chat transports make it worse. Each card is a message in the thread, and an
  operator who approves the first and walks away leaves the rest pending until
  they expire.

The predictable result is approval fatigue: people approve cards without
reading them, which defeats the gate's purpose more thoroughly than any bypass.

### Why not just widen the window?

A longer single-action window does not help: the arguments of the next step
are not known when the first card is shown. "Approve everything for 15 minutes"
is a session-level bypass, the shortcut a gate must never offer. Every
mutating tool becomes unattended for the duration.

## Proposed Solution

Let the agent **declare a plan**, and let the human approve the plan, not each
step: a stated intent plus the closed set of guarded tools it will need, valid
for one window, for one requester, in one scope.

### Step 1: A `propose_plan` tool

```python
@confirm("asks the user to approve a multi-step change plan")
async def propose_plan(summary: str, steps: list[str], tools: list[str]) -> dict: ...
```

The agent calls it before a multi-step change. The gate renders the plan (the
summary, the numbered steps, and the tool *names*) as one approval card.
`tools` is validated at proposal time against the guarded tools the proposing
agent can actually reach. An unknown name is refused with near-miss
suggestions, never silently dropped, so a typo cannot produce a plan that
covers nothing.

### Step 2: Plan entries in the confirmation store

A `PendingConfirmation` gains `kind="plan"` and `plan_tools: frozenset[str]`.
Approving one marks it approved for `ORRERY_PLAN_VALIDITY_SECONDS` (default 900).
While it is valid, the confirmation gate treats a guarded call by the **same
requester in the same scope** to a tool **in `plan_tools`** as approved,
whatever its arguments. Every such call:

- is recorded as a plan step in the audit log (AEP-024), with the plan's
  `action_id`, so the record shows which approval authorised which call;
- still passes RBAC, the autonomy level and namespace scope. A plan widens
  *confirmation* only, never *authorization*.

### Step 3: What a plan never covers

- **`@destructive` tools are excluded by default.** A plan covers `@confirm`
  tools. Including destructive ones needs `ORRERY_PLAN_ALLOW_DESTRUCTIVE=true`,
  and even then each destructive call is listed on the card by name.
- **Unattended runs** (`run_triage.py`, scheduled sweeps) cannot propose or hold
  plans. There is no human to approve them, and a plan must never be the way an
  unattended path gains a mutation.
- **Another person's approval.** The requester-verified rule is unchanged:
  only the requester may approve a plan, and anyone may deny it.

### Step 4: Equivalent tools

A plan that names `scale_deployment` should not fail on the agent choosing
`patch_deployment` to set replicas. Rather than guessing, keep an explicit,
reviewed `EQUIVALENT_TOOLS` map (`scale_deployment ↔ patch_deployment`), so
"the plan covered it" stays a reviewable claim.

### Step 5: End-of-turn ledger

`ToolLedgerPlugin` already records which guarded calls ran. At the end of a turn
under an approved plan, the gateway can compare the plan's tools with what
actually ran. Log it, and count it as a metric; do **not** add it as a banner.
An approved step that did not run is a weak signal: the agent may have reached
the same outcome another way. Banners that cry wolf train people to ignore
the one that matters.

## Acceptance Criteria

- [ ] `propose_plan` validates tool names and refuses unknown ones with suggestions
- [ ] An approved plan authorises its declared `@confirm` tools for the requester, in its scope, for its window
- [ ] RBAC, autonomy and namespace scope still apply to every plan step
- [ ] `@destructive` tools are excluded unless explicitly enabled
- [ ] Unattended runs cannot propose or use a plan
- [ ] Every plan step is audited against the plan's `action_id`
- [ ] Spent plans are pruned (the Postgres backend must not accumulate approved rows)

## Notes

- Retention matters on the shared backend. An approved plan is not popped on use
  (its tools may run several times), so the store must prune spent plans
  explicitly. Otherwise every confirmation operation drags a growing table
  behind it.
- Plan TTLs should be longer than single-action TTLs, both for the unanswered
  card (people read a plan before deciding) and for the approval (a sequence
  takes minutes).
