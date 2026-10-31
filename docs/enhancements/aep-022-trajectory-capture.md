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
hypothesis, how much help did they take. When the learner finishes an
attempt, Orrery produces a **structured debrief** that only the lab can read,
and the learner's browser carries it to the lab.

### Threat model in one line

The learner is the adversary for exam integrity. A debrief the learner can
read is an **answer oracle**: `first_correct` says whether any message so far
held the target, so "is it 4800, 4900, 5000, 5100?" followed by *Finish*
narrows the value down. So the debrief is **encrypted to the lab**, finishing
is **one-shot**, and each attempt the lab issues is **bound to exactly one
session from the moment that session is created**.

### What the debrief contains, and where each field comes from

Every field is derived from the **session store**, so a debrief survives a
restart once the lab runs on PostgreSQL (the learning sandbox allows
`DATABASE_URL` and keeps no long-term memory).

| Field | Source |
|-------|--------|
| `turns` | learner message events |
| `delegations`: `{t_ms, specialist, outcome}` | the root's `AgentTool` calls and responses. `outcome` is a **closed enum**: `ok`, `refused` (a gate's or a run budget's status), `error`. It is never text from the response, which is where values live |
| `levels`: `{t_ms, level}` | the level the server stamps into each turn's `state_delta`, stored on the learner's message event. **Coarse:** it is the per-learner level at the start of each turn, so a hint shows at the next turn, and a level reached in an earlier session of the scenario shows from turn 1. Labs charge hint costs from the `learning_hint` audit stream, which is the record of each hint |
| `first_correct`: `{target_id, t_ms, after_reveal}` per target | the first learner message the grading matcher finds the target in. `after_reveal` is true when level 3 was stamped first |
| `hypotheses`: `[{hypothesis_id, t_ms}]` | learner messages matched against the manifest's optional `hypotheses` list (below) |
| `evidence_before_conclusion` | whether a delegation to the target's `specialist` (manifest v2, below), or to `incident_triage_agent` (whose sweep covers it), ran before `first_correct` |
| `wrong_candidates_before_correct` per target | how many learner messages before `first_correct` named a different value of the same magnitude: "4800?", "4900?", "5000?", "5100?" counts 3. The lab decides what that costs |

**The grading matcher is stricter than the guard's.** The answer guard is
lenient on purpose ("5k" counts as stating 5100). For grading, that would
reward a shotgun list, so a message naming two or more candidates of the same
magnitude does not count as a correct hypothesis.

**Manifest contract v2** (v1 still accepted) adds, all optional:

- `targets[].specialist`: the specialist key whose evidence supports the
  target (`kafka`), for `evidence_before_conclusion`;
- `hypotheses: [{id, descriptor, keywords: [...]}]`: the plausible causes the
  scenario author lists. A learner message with a keyword counts as trying
  that hypothesis. This is crude, but deterministic, reviewable and free. Ids
  and keywords pass the same validator as target ids (no value inside them).

The specialists' own tool calls run in `AgentTool` sub-runs whose events the
root session never holds. Per-tool steps would need a session-backed recorder
like `ToolLedgerPlugin`, which is phase 2.

### What it never contains

No message text, tool arguments, tool results or model output, and no target
value (`first_correct` names the target id only).

### Transport: finish once, encrypted to the lab, relayed by the browser

Orrery never lets a service principal read a person's session (ADR-006), so
the lab does not pull. Instead:

1. **The lab issues the attempt as a token Orrery verifies, and the first
   turn binds it.**
   - When it launches the exercise, the lab seals an **attempt token** under
     the attempt key (JWE, same scheme as the debrief, protected header
     `typ: orrery-learning-attempt`):
     `{attempt_id, iss, sub, scenario_id, issued_at, exp}`. It passes the
     token in the launch URL's **fragment** (`#attempt=…`), which never
     reaches Orrery's or the ingress's access logs. The console reads it,
     removes it with `history.replaceState`, and the page is served with
     `Referrer-Policy: no-referrer`.
   - The console sends it with the **first** message of a new conversation.
     Orrery decrypts it and checks four things before binding: `typ` is
     `orrery-learning-attempt`, `iss`/`sub` are the caller's, `scenario_id`
     is the manifest's, and `issued_at` is after this process started while
     `exp` is still in the future. A made-up or foreign id cannot open a
     session.
   - The binding is a server-set, guarded key in the session's state
     (`_learning_attempt`, in `GUARDED_STATE_KEYS`) plus an
     `attempt_id -> session_id` record kept by the process. The record is
     checked and set with no `await` in between, which is what stops two
     concurrent first turns in two **different** sessions from both binding:
     the turn lock is per `(user, session)`, so it only orders turns within
     one session.
   - A second session presenting a bound `attempt_id` is refused. Every turn
     in a learning deployment needs a bound attempt, so there is no unbound
     session to work the answer out in.
   - **Restarts.** The record is per process (learning mode already refuses
     `ORRERY_MULTI_REPLICA`), so a restart forgets it. That is why a token
     issued before the process started is refused: otherwise a learner could
     work the answer out in session A, delete A, and after a restart bind
     the same token to a clean session B. A session already bound keeps
     working after a restart (its `_learning_attempt` is in the session
     store); a learner who lost theirs relaunches, the lab issues a new
     attempt, and the old one counts as failed (step 6). No store search,
     and so no `await` between check and set. The lab also learns the bound
     `session_id` from the debrief and refuses a debrief from any other
     session of that attempt.
2. **The learner finishes once.** *Finish exercise* calls
   `POST /learning/attempts/{attempt_id}/finish` with the `session_id`, with
   `Cache-Control: no-store` and no CORS. The call needs all of:
   - learning mode;
   - a verified **human** principal that owns the session (`owns_session`);
   - a session that is **not** a machine session (`is_machine_session()`), so
     an agent acting for the learner cannot do the exercise for them.

   The `attempt_id` must be the one bound to that session. The server takes
   the session's turn lock and writes a server-set `_learning_finished`
   marker (in `GUARDED_STATE_KEYS`). The refusal of further turns is checked
   **inside** the turn lock, in `run_in_session`, not in `inbound()`, which
   runs before the lock: a turn queued behind the finish then sees the
   marker. A second finish is refused. One debrief per attempt. Orrery's
   record of used attempts is per process, so the **lab's** single-use check
   is the authoritative one.
3. **Encrypted, not just signed.** The payload holds `iss`, `sub`,
   `scenario_id`, `attempt_id`, `session_id`, the session's first-event time,
   `issued_at`, a random `nonce` and the fields above. It is sealed with
   AES-256-GCM (JWE `dir` + `A256GCM`, protected header
   `typ: orrery-learning-debrief`) under `ORRERY_LEARNING_DEBRIEF_KEY`:
   env only, 32 bytes, and startup fails if it is missing while debriefs are
   on. The browser gets an opaque token it can neither read nor edit.
   `iss` and `sub` go in clear inside the ciphertext, so no unsalted hash
   needs matching, and pairwise-subject IdPs keep working.
   - **One key per direction.** The attempt key
     (`ORRERY_LEARNING_ATTEMPT_KEY`; the lab seals, Orrery opens) and the
     debrief key (Orrery seals, the lab opens) are different 32-byte keys,
     and startup refuses them equal. Each verifier also requires its own
     `typ`, so an attempt token posted to the lab's debrief endpoint (or
     the reverse) fails even under a lenient verifier. Both are shared with
     the lab and nobody else; a symmetric key lets either holder seal, which
     is acceptable since the lab is the only other party.
   - Each JWE header carries a `kid`, so a new key
     (`ORRERY_LEARNING_ATTEMPT_KEY_ID`, `ORRERY_LEARNING_DEBRIEF_KEY_ID`)
     can be rolled in while the previous one is still accepted.
4. **Relay without the lab's cookies.** The console POSTs the token to
   `ORRERY_CONSOLE_DEBRIEF_URL` (https, validated like
   `ORRERY_CONSOLE_ACCESS_DENIED_URL`, plus a CSP `connect-src` entry) as a
   **non-credentialed** request. The token authenticates itself: it is
   sealed with the lab's key and names the attempt, so the lab needs no
   session cookie, `SameSite=None` or credentialed CORS. It only allows the
   console origin.
5. **The lab accepts it** when:
   - it decrypts under the debrief key with `typ: orrery-learning-debrief`;
   - its `attempt_id` is one the lab issued and has not used;
   - its `iss`/`sub` and `scenario_id` are the ones that attempt was issued
     for (so an easy scenario's session can't be finished under a hard one's
     attempt);
   - the session started after the attempt was issued;
   - `issued_at` is within a window.

   It then marks the attempt used. Working the answer out in session A and
   typing it in session B fails: an attempt binds to the first session that
   uses it, and every turn needs a bound attempt.
6. **A missing debrief is a failed attempt, and attempts are graded
   together.** The learner's browser carries the token, so the learner can
   withhold it. The lab treats an issued attempt with no accepted debrief
   inside its window as not passed. Since a relaunch is a fresh, clean
   session, the lab **grades all of a learner's attempts on a scenario
   together** (and may cap how many it issues). An attempt spent working the
   answer out, then withheld, still counts as a failed attempt, so a quick
   second one does not erase it.

### Phase 1 deliverables

- `orrery_core/learning/debrief.py`: derives the fields from session events,
  with the grading matcher, and never copies a value out.
- Attempt-token verification and binding on the first turn
  (`_learning_attempt`), and
  `POST /learning/attempts/{attempt_id}/finish` with the `_learning_finished`
  marker, refused inside the turn lock. `ORRERY_LEARNING_ATTEMPT_KEY`,
  `ORRERY_LEARNING_DEBRIEF_KEY` and their `_KEY_ID`s.
- Manifest contract v2 (`targets[].specialist`, `hypotheses`).
- Console: the attempt token from the launch URL's fragment (then removed
  from the address bar), and *Finish exercise*,
  posting to `ORRERY_CONSOLE_DEBRIEF_URL`.
- A reference decrypt-and-verify snippet for labs (Python and TypeScript) in
  `docs/integrations/`.

### Acceptance criteria

- [ ] A non-owner, a non-human principal, a machine session and a
      non-learning deployment are refused. No route exists outside learning
      mode.
- [ ] A turn without a verified, bound attempt is refused. A made-up,
      expired, foreign-subject or other-scenario attempt token cannot open
      a session. A second session presenting a bound `attempt_id` is
      refused. After a restart, a token issued before the process started
      is refused, while a session bound before it keeps working.
- [ ] An attempt token is refused by the debrief verifier and a debrief by
      the attempt verifier (`typ`), and equal attempt and debrief keys stop
      startup.
- [ ] Two concurrent first turns, in one session or in two, cannot both
      bind one attempt.
- [ ] After a finish the session refuses turns, including one already
      waiting on the turn lock. A second finish is refused.
- [ ] The browser-held token reveals nothing: decrypting without the key
      fails, and tampering with any byte fails authentication.
- [ ] A seeded target value in a tool result, a model reply or a learner
      message never appears in a debrief (fuzzed like the guard tests).
- [ ] A message listing several same-magnitude candidates is not a correct
      hypothesis, and candidates spread over several messages are counted
      in `wrong_candidates_before_correct`.
- [ ] `first_correct.after_reveal` is true when level 3 was stamped before
      the correct message.
- [ ] After a restart, a debrief rebuilt from a PostgreSQL session store has
      the same fields, apart from `issued_at`, `nonce` and the ciphertext.
