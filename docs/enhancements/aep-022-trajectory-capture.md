# AEP-022: Trajectory Capture & Eval Harvesting

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | Medium (3-4 days) |
| **Impact** | Medium-High |
| **Dependencies** | AEP-002 (eval framework) — strong; AEP-013 (PII redaction) — strong; AEP-032 phase 2 (hint ladder) — for the learner debrief phase |

> Pattern borrowed from the Hermes agent architecture (ShareGPT trajectory
> generation for training data). Retargeted at Orrery's biggest maintenance pain:
> hand-authoring eval scenarios.

## Gap Analysis

### Current Implementation

Orrery records tool activity in two places, but neither produces a **structured,
replayable trajectory**:

- `ActivityPlugin` (`core/orrery_core/observability/activity.py`) appends a
  one-line summary per tool call to `session.state["session_log"]` — human-
  readable, lossy (`[agent] argsummary → status`), not a machine trajectory.
- `AuditPlugin` emits per-tool audit records to the log stream — good for
  forensics, not shaped for replay or dataset use.

Eval scenarios (`agents/*/tests/evals/*.test.json`) are therefore **written by
hand**: a human invents the prompt and types the expected `tool_uses` and args.
As the AEP-002 work and the recent eval-hardening pass showed, this is slow,
error-prone, and drifts as models change.

### Why this matters now

We *just* spent real effort refreshing expected trajectories by running agents by
hand and reading off their tool calls. That manual loop is exactly what a
trajectory exporter automates: capture a real (mocked-backend) run once, and emit
a ready-to-commit `*.test.json` scenario. Two compounding payoffs:

1. **Eval authoring becomes record-and-edit** instead of write-from-scratch.
2. A corpus of real trajectories becomes available for future fine-tuning /
   distillation to a cheaper model.

### What's available

- ADK invocation events already carry the full `(user_content, tool_uses, args,
  final_response)` shape the eval format needs — the same data ADK's
  `AgentEvaluator` consumes.
- `PIIRedactionPlugin` (AEP-013) already scrubs credentials from tool results, so
  exported trajectories are safe to persist by construction.
- The eval dataset schema is a known, small JSON shape (see AEP-002).

## Proposed Solution

An opt-in exporter plugin that serializes each invocation to a structured
trajectory, plus a CLI to convert captured trajectories into eval scenarios.

### Step 1: A trajectory record

Add `core/orrery_core/observability/trajectory.py`:

```python
@dataclass
class ToolStep:
    name: str
    args: dict[str, Any]
    status: str


@dataclass
class Trajectory:
    invocation_id: str
    agent: str
    prompt: str
    steps: list[ToolStep]
    final_response: str
    at: float

    def to_eval_case(self, eval_id: str) -> dict:
        """Emit an ADK AgentEvaluator `*.test.json` eval_case."""
        return {
            "eval_id": eval_id,
            "conversation": [
                {
                    "user_content": {"parts": [{"text": self.prompt}]},
                    "final_response": {"parts": [{"text": self.final_response}]},
                    "intermediate_data": {
                        "tool_uses": [{"name": s.name, "args": s.args} for s in self.steps],
                        "intermediate_responses": [],
                    },
                }
            ],
        }
```

### Step 2: Capture plugin (off by default)

```python
class TrajectoryCapturePlugin(BasePlugin):
    """Serializes each invocation's prompt → tool calls → response to a JSONL sink."""

    def __init__(self, sink: TrajectorySink):
        super().__init__(name="trajectory_capture")
        self._sink = sink
        self._steps: dict[str, list[ToolStep]] = defaultdict(list)

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        # Runs AFTER PIIRedactionPlugin (registration order), so args/result are scrubbed.
        self._steps[tool_context.invocation_id].append(
            ToolStep(name=tool.name, args=dict(tool_args), status=_status_of(result))
        )
        return None

    async def after_run_callback(self, *, invocation_context):
        traj = Trajectory(
            invocation_id=invocation_context.invocation_id,
            agent=invocation_context.agent.name,
            prompt=_first_user_text(invocation_context),
            steps=self._steps.pop(invocation_context.invocation_id, []),
            final_response=_final_text(invocation_context),
            at=time.time(),
        )
        await self._sink.write(traj)
```

Sinks: `JsonlFileSink` (local dev, one line per run) and an optional
`PostgresTrajectorySink` (reuses `DATABASE_URL`) for fleet-wide capture.

### Step 3: Harvest CLI — trajectory → eval scenario

```bash
# Convert captured runs into a reviewable eval dataset
uv run python -m orrery_core.observability.harvest \
    --input trajectories.jsonl \
    --agent kafka_health_agent \
    --out agents/kafka-health/tests/evals/harvested.test.json
```

The CLI groups by agent, de-dupes near-identical prompts, and emits a
`*.test.json` a human then **reviews and trims** before committing — capture
proposes, a human disposes (never auto-commit an eval).

### Step 4: Opt-in wiring

```python
def default_plugins(..., capture_trajectories: bool | None = None):
    if capture_trajectories or os.getenv("ORRERY_TRAJECTORY_CAPTURE") == "true":
        plugins.append(TrajectoryCapturePlugin(_sink_from_env()))
```

Off by default; enabled in dev/eval-harvest runs, or in a sampled fashion in
production (`ORRERY_TRAJECTORY_SAMPLE=0.01`).

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/observability/trajectory.py` | New — `Trajectory`, `ToolStep`, `to_eval_case()`, sinks |
| `core/orrery_core/observability/harvest.py` | New — CLI: trajectories → `*.test.json` |
| `core/orrery_core/plugins/trajectory_plugin.py` | New — `TrajectoryCapturePlugin` |
| `core/orrery_core/plugins/__init__.py` | Wire behind `ORRERY_TRAJECTORY_CAPTURE` (after PII redaction) |
| `core/tests/test_trajectory.py` | New — capture shape, `to_eval_case` round-trips through `AgentEvaluator` |
| `docs/evals.md` | Add a "Harvesting scenarios from real runs" section |

## Acceptance Criteria

- [ ] `TrajectoryCapturePlugin` serializes prompt → ordered tool calls+args → final response
- [ ] Plugin registers **after** `PIIRedactionPlugin` so captured args/results are scrubbed
- [ ] `Trajectory.to_eval_case()` output loads and runs under ADK's `AgentEvaluator`
- [ ] Harvest CLI groups by agent, de-dupes, writes a reviewable `*.test.json`
- [ ] JSONL sink for local dev; optional Postgres sink behind `DATABASE_URL`
- [ ] Off by default; enabled via env, with optional sampling in production
- [ ] Docs explain the capture → review → commit loop (never auto-commit)

## Notes

- **Never auto-commit harvested evals.** Capture proposes candidate scenarios; a
  human must review the trajectory (is this behavior *correct*, or a bug we'd be
  freezing in?) before it becomes a regression test. This is the same lesson from
  the eval-hardening pass: a trajectory is only a good expectation if the behavior
  is genuinely right.
- Redaction is load-bearing here — trajectories persist tool output, so capture
  **must** sit downstream of `PIIRedactionPlugin`; a unit test should assert a
  seeded credential never appears in a captured trajectory.
- The fine-tuning corpus is a bonus, not the driver — the immediate win is
  killing the hand-authoring of eval scenarios.

## Phase 1: the learner debrief (AEP-032 follow-up)

> Status: **proposed**, scope only. Nothing here ships in the next release.
> It builds on the learning sandbox's hint ladder (AEP-032 phase 2).

### Goal

A practice lab grades the *process*, not only the final answer: did the
learner gather evidence before concluding, how long until the first correct
hypothesis, how much help did they take. At the end of an exercise the
learner's own browser fetches a **signed, structured debrief** of their
session and hands it to the lab, which scores it and feeds spaced review.

### What the debrief contains, and where each field comes from

Every field is derived from the **session store**, not from a new in-process
recorder, so a debrief survives a restart once the lab runs on PostgreSQL
(the learning sandbox allows `DATABASE_URL` and keeps no long-term memory).

| Field | Source | Deterministic? |
|-------|--------|----------------|
| `turns` | learner message events | yes |
| `delegations`: `{t_ms, specialist, outcome}` | the root's `AgentTool` function calls and responses in the session events | yes |
| `hints`: `{t_ms, level}` | the level the server stamps into each turn's `state_delta` (AEP-032 §3), persisted with the learner's message event | yes |
| `first_correct`: `{target_id, t_ms, after_reveal}` per target | the first learner message the answer guard's matcher (`states_value`) finds the target in. `after_reveal` is true when level 3 was reached first | yes |
| `hypotheses`: `[{hypothesis_id, t_ms}]` | learner messages matched against the manifest's optional `hypotheses` list (below) | yes (keyword match) |
| `evidence_before_conclusion` | whether a delegation whose specialist owns the target ran before `first_correct` | yes |

The specialists' own tool calls run in `AgentTool` sub-runs whose events the
root session never holds. Phase 1 reports delegations only. Per-tool steps
would need a recorder like `ToolLedgerPlugin` writing to the session store,
which is phase 2.

**Hypotheses without a model.** Manifest contract **v2** adds an optional
`hypotheses: [{id, descriptor, keywords: [...]}]`. The scenario author lists
the plausible causes (lag, broker down, OOM), and a learner message mentioning
a keyword counts as that hypothesis being tried. That is crude, but it is
deterministic, reviewable and free. A model judge mapping free text onto the
same closed list is possible later, opt-in; it never invents hypothesis ids.

### What it never contains

- No message text, tool arguments, tool results or model output. Those are
  where target values live, and a debrief is handed to a third party.
- No target value. `first_correct` names the target id only.
- No subject in clear: `subject_sha256` only. The lab already knows its
  learner's OIDC `sub` and compares hashes.

### Transport: owner-only and signed, relayed by the learner's browser

Orrery never lets a service principal read a person's session (ADR-006), so
the lab does not pull. Instead:

1. The console shows **Finish exercise**, which calls
   `GET /learning/debrief/{session_id}`. The call needs learning mode, a
   verified **human** principal, and a session that principal owns (the same
   `owns_session` rule as `GET /session/{id}`).
2. The server builds the debrief, puts `issued_at` and a random `nonce` in
   it, and signs `HMAC-SHA256(secret, canonical_json(payload))`. The
   payload covers `session_id`, `subject_sha256`, `scenario_id`,
   `issued_at`, `nonce` and every field above. The secret is
   `ORRERY_LEARNING_DEBRIEF_SECRET`: env only, at least 32 bytes, and
   startup fails if it is missing while debriefs are enabled.
3. The browser POSTs `{payload, signature}` to the lab's API with the
   learner's lab session.
4. The lab verifies the HMAC, checks `subject_sha256` against its own
   learner, checks `scenario_id` and an `issued_at` window, and stores the
   `nonce` so a replayed debrief is refused. A later debrief of the same
   session replaces the earlier one.

The browser cannot forge or edit a debrief without the secret. It can only
choose not to send one, and the lab treats that as "not finished".

### Phase 1 deliverables

- `orrery_core/learning/debrief.py`: derives the fields from session events.
  It reuses `states_value` and the manifest, and never copies a value out.
- `GET /learning/debrief/{session_id}` (learning mode only, human owner
  only, rate-limited), plus `ORRERY_LEARNING_DEBRIEF_SECRET`.
- Manifest contract v2 (`hypotheses`), with v1 still accepted.
- Console: a **Finish exercise** button that fetches the debrief and posts
  it to `ORRERY_CONSOLE_DEBRIEF_URL` (https, validated like
  `ORRERY_CONSOLE_ACCESS_DENIED_URL`), with a CSP `connect-src` entry for
  that origin.
- A verification snippet for labs (Python and TypeScript) in
  `docs/integrations/`.

### Acceptance criteria

- [ ] A non-owner, a non-human principal and a non-learning deployment all
      get 404/403. No route exists outside learning mode.
- [ ] A seeded target value in a tool result, a model reply or a learner
      message never appears in a debrief (fuzzed like the guard tests).
- [ ] Tampering with any payload byte fails verification; a reused nonce is
      refused by the reference verifier.
- [ ] `first_correct.after_reveal` is true when level 3 preceded the
      correct message.
- [ ] After a restart, a debrief built from a PostgreSQL session store is
      identical to one built before the restart.
