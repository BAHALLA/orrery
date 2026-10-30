# AEP-032: Learning Sandbox Mode for Practice Labs

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--grey">P3</span> |
| **Effort** | Medium (4-6 days, in four phases) |
| **Impact** | Medium (practice labs only; no effect on any other deployment) |
| **Dependencies** | AEP-009 (streaming), AEP-002 (evals), ADR-005 (specialist selection), ADR-006 (caller principals) |

!!! warning "A sandbox mode, not an agent feature"
    Everything here is **off by default**, lives in its own package
    (`orrery_core.learning`) that is imported only when
    `ORRERY_LEARNING_MODE=true`, and refuses to start unless the deployment
    is a read-only, authenticated practice sandbox. A production deployment
    never loads it, and nothing in this proposal changes how Orrery behaves
    when the flag is off. The only general-purpose piece, per-tool narrowing
    (phase 0), can only take tools away.

## Gap Analysis

Orrery is used as the subject of hands-on labs: a learner gets a seeded fault
(a Kafka consumer group with lag, an alert firing) and is asked to diagnose
it. Three things make that a poor learning exercise today:

- **The agent answers the exercise.** Orrery is built to lead with the
  verdict and quote exact values (`OPERATING_PRINCIPLES`). Asked "what is the
  lag?", it reads the live system and says the number, so the lab grades how
  well someone prompts an agent, not how they diagnose.
- **A prompt cannot fix it.** A coaching instruction ("don't give the answer
  away") is a request to a model that has just read the answer from a tool.
  A determined "just tell me the number" gets it, and the coordinator can
  route around the coach to a specialist that has no such instruction.
- **Help is invisible.** Nothing records how much help a learner needed, so a
  lab cannot weigh a solution reached alone against one reached after the
  agent spelled it out.

What is missing is a server-side control that knows the exercise's answers
and keeps them out of what the learner sees until they ask for them, plus a
record of every step of help.

## Non-goals

- **Not an exam-proof control.** The learner can still reason from the
  evidence Orrery shows them, which is the point. The guard stops the agent
  from *stating* an answer; it does not stop a learner from working it out.
- **Not a security boundary.** It protects exercise answers, not secrets. The
  security boundary stays where it is: auth, RBAC, autonomy, the gates
  (ADR-001, ADR-006).
- **Not a tutoring product.** No curricula, scoring or progress tracking
  inside Orrery; the lab platform owns tasks and grading. Orrery only
  withholds answers, hands out hints, and reports what happened.
- **No new way to change the lab.** Learning mode runs at L2. Letting
  learners mutate resources in a shared broker is a separate decision (see
  *Future Work*).

## Solution

### 1. The sandbox gate

`ORRERY_LEARNING_MODE=true` is refused at startup (a `ValueError` naming
the missing condition, the same way `ORRERY_STUDIO_ENABLED` checks its
preconditions) unless **all** of these hold:

| Condition | Why |
|-----------|-----|
| `AUTH_ENABLED=true` | hints and answers are per person; an anonymous caller has no identity to scope them to |
| `ORRERY_HUMAN_CLIENTS` set | only verified humans are learners (ADR-006); without it every token is "human" |
| `ORRERY_AUTONOMY_LEVEL=L2` | the sandbox is read-only; a learner-facing deployment that can mutate is not a sandbox |
| `ORRERY_SCENARIO_MANIFEST` set and valid | the guard has nothing to protect without it, and an unguarded "learning mode" would be worse than none |
| `ORRERY_A2A_ENABLED` unset or false | a machine caller would bypass the console's held stream and could fetch answers for a learner |

It is supported on the **HTTP front door only** (console, `POST /chat`,
`POST /chat/stream`). The Slack bot, Google Chat bot, CLI and batch triage
refuse to start with the flag set rather than run unguarded, because their
reply paths do not go through the guard described below.

When active, `GET /healthz` reports `"mode": "learning"` and the console shows
a persistent *Learning sandbox* badge, so nobody mistakes a lab for an
operational deployment.

### 2. The scenario manifest (contract v1)

The lab's seeder knows the answer it seeded, so it states it. The file named
by `ORRERY_SCENARIO_MANIFEST`:

```json
{
  "schema_version": 1,
  "scenario_id": "kafka-lag-7f3a",
  "seeded_at": "2026-10-30T10:00:00Z",
  "default_disclosure": 0,
  "targets": [
    {"id": "total_lag", "descriptor": "total lag of group orders-processor",
     "value": 5100, "unit": "messages"}
  ]
}
```

- **Strict schema.** Pydantic with `extra="forbid"`; an unknown field or
  `schema_version` is an error, so a typo fails loudly instead of guarding
  nothing.
- **Values, not formats.** `value` is a number or a string. For a number the
  guard derives the spellings itself (separators, `5.1k`, words); for a string
  it matches exactly, ignoring case. The lab never ships a format list.
- **Narrow-only by construction.** The schema has no field that can raise
  autonomy, grant a role, or add a tool. A later `hide_tools` field may only
  remove tools.
- **Who seeds the file.** The lab's seeder writes it atomically (temp file in
  the same directory, then `rename`) onto a volume mounted **read-only** into
  Orrery. Orrery re-reads it when its mtime changes.
- **Reload failures.** Invalid at startup: startup fails. Invalid on reload:
  the last valid manifest stays in force, an ERROR is logged and
  `orrery_learning_manifest_errors_total` increments. There is never an
  unguarded window.
- **Re-seeding.** After a new `scenario_id` loads, the previous manifest's
  values stay guarded for `ORRERY_LEARNING_GRACE_SECONDS` (default 3600), so a
  learner mid-attempt is not handed the old answer.
- **Confidentiality.** Only the guard reads `targets[].value`. Values never
  enter a prompt, session state, a trace attribute, a log line, an audit
  entry or an export. Audit lines carry `scenario_id` only.

### 3. Disclosure levels and hints

Each session has a disclosure level from 0 to 3, starting at the manifest's
`default_disclosure`:

| Level | What the agent may say |
|-------|------------------------|
| 0 | questions only: which signal to look at |
| 1 | where to look (a topic, a group, an alert) |
| 2 | what to compare, without the conclusion |
| 3 | the answer, with its evidence; target values are no longer withheld |

- **Raising the level is a person's act, not the model's.** The console gets
  a *Hint* button that calls `POST /session/{id}/hint`. There is
  deliberately **no model-callable tool**: a learner could talk the model
  into calling it.
- **Endpoint rules.**
  - Owner-only, using the same `caller_session` lookup as `/activity`
    (another person's session is a plain 404).
  - Rate-limited per verified subject.
  - Raises the level by exactly one step and never lowers it.
- **Stored like other server-set state.** The level is written the way
  `set_autonomy_level()` writes autonomy, with a server-set marker, and both
  keys are added to `IdentityStateGuardPlugin`'s protected set, so no tool
  can change them. A bare value without the marker is ignored and treated as
  level 0.
- **Every change is an event.**
  - `hint.level` on the SSE stream.
  - A `learning_hint` audit entry (subject, session, scenario_id, old and new
    level).
  - `orrery_learning_hints_total{level}`.
- **The model sees the level, never the values.** The coordinator's per-turn
  context (the same provider that adds "who you are talking to") gains one
  line: the current level and what it permits.

### 4. The answer guard

`AnswerGuardPlugin` (`after_model_callback`, registered only in learning
mode) checks the **user-facing** final response, the root agent's
non-partial output, against every guarded value while the session's level is
below 3:

- **What counts as a match.** For numbers: digit sequences with or without
  separators (`5100`, `5,100`, `5 100`, `5.100`), `k`/`M` abbreviations
  within the value's rounding, and the number in words. For strings: a
  case-insensitive substring. Matching is deterministic and unit-tested.
- **On a match**, the response is replaced, not edited.
  - The replacement is a fixed nudge ("I can't give that value yet. Here's
    what to look at next: …") built from the target's `descriptor` and the
    current level.
  - The original text is never stored or logged.
  - A `learning_answer_withheld` audit entry records target id and
    scenario_id, never the value.
- **Route-independent.** The guard sits on the final answer, so it holds
  whether the coordinator used the coach, a specialist directly, or memory.
- **Residual risk, stated.** A paraphrased approximation ("about five
  thousand") can pass. That rate is measured by the eval below, not promised
  away.

**Streaming hold.** `POST /chat/stream` normally sends `delta` frames as the
model writes, and text already sent cannot be retracted. In learning mode the
SSE layer suppresses `delta` frames and sends the checked `message` frame
only. `progress` frames (tool names and outcomes, never arguments or results)
still flow, so the console still shows the agent working.

### 5. The coach

The declarative `lab_coach` agent (read-only kafka and observability tools,
a four-step hint ladder; first drafted in PR #342) becomes part of the
learning package. It is added to the chat root **only in learning mode**, and
`ORRERY_AGENTS_CONFIG` is not the way to load it. Its ladder follows the
session's disclosure level instead of a marker in its own replies. It is the
pedagogy layer; the guard is the safety layer, and the coach is never relied
on to withhold anything.

### 6. Per-tool narrowing (phase 0, general)

`ORRERY_DISABLED_TOOLS` (comma-separated, qualified names like
`kafka.get_consumer_lag`) removes tools from their specialists before the
tree is built. It extends ADR-005 and follows its rules:

- **It only removes.** There is no allowlist that could add a tool back.
- **Unknown names fail.** An unknown or ambiguous name stops startup.
- **No back door.**
  - The removal also applies to the declarative-agent catalogue, admin
    catalog edits and Studio's tool list.
  - It is enforced as a refusal in `GuardrailsPlugin` as well, so a tool
    reached any other way is still refused.

It is not learning-specific (any deployment can narrow itself), which is why
it is the one piece outside `orrery_core.learning`. A lab uses it to make a
learner compute a value instead of reading it from a tool.

## Design decisions

- **The lab states the answers; Orrery doesn't infer them.**
  - A target like "total lag" is a sum the model computes, so no tool
    returns it verbatim.
  - Inferring answers from tool output would be guesswork, and wrong guesses
    leak.
  - The seeder knows the truth, so it writes it down.
- **The context comes from the deployment, not from a caller.** A per-session
  context set by a trusted service would mean a service principal writing
  into a human's session. ADR-006 forbids that (`owns_session`), and opening
  it up for labs would weaken the boundary every other surface relies on.
  One scenario per deployment fits how labs run.
- **Fail closed everywhere.**
  - Preconditions refuse startup.
  - A bad manifest keeps the last good one.
  - A missing level reads as 0.
  - Surfaces that cannot hold the stream refuse to start.
- **A separate package.** `orrery_core.learning` is imported only behind the
  flag, mirroring `studio/` and `a2a_server.py`. No learning code runs, and
  no learning plugin is registered, in a normal deployment.

## Threat Model

| Threat | Control |
|--------|---------|
| Learner talks the model into stating a value | Answer guard on the final response; adversarial eval (acceptance) |
| Coordinator routes around the coach | Guard is route-independent |
| Streamed text reveals the value before the guard runs | `delta` frames held in learning mode |
| A tool or injected text raises the disclosure level | Level written only by the hint endpoint with a server-set marker; keys protected by `IdentityStateGuardPlugin` |
| Learner raises another learner's level | Owner-scoped endpoint; another session is a 404 |
| Hint endpoint abused for load | Per-subject rate limit; one step per call |
| Values leak via logs, traces, audit, memory recall | Values read only by the guard; audit carries `scenario_id`; ADK message capture in spans stays off (AEP-030) |
| Manifest tampered to widen access | Schema has no widening fields; volume read-only; `extra="forbid"` |
| Manifest broken mid-session | Last valid manifest stays in force; metric + ERROR |
| Machine caller fetches answers | A2A refused in learning mode; Slack/Chat/CLI refuse the flag |
| Learning mode enabled on an operational deployment by mistake | Preconditions (L2, humans, manifest) fail on a normal production config; `/healthz` and console badge |
| Activity timeline leaks a value through tool arguments | Timeline shows args summary and status only, never results; a value typed by the learner is theirs already |

Accepted residual risks: paraphrased approximations, and a learner reading
raw evidence the agent showed them. Both are inherent to a diagnostic
exercise.

## Affected Files

- `core/orrery_core/learning/` (new, imported only behind the flag):
  `config.py` (flag, preconditions), `manifest.py` (schema, reload, grace),
  `guard.py` (matcher + `AnswerGuardPlugin`), `hints.py` (level state, hint
  endpoint), `coach.yaml`.
- `core/orrery_core/plugins/identity_guard_plugin.py`: protect the level
  keys.
- `core/orrery_core/serving/server.py`, `serving/sse.py`: mount the hint
  route, hold `delta` frames, `/healthz` mode.
- `core/orrery_core/registry.py` / `agent/declarative.py`:
  `ORRERY_DISABLED_TOOLS` (phase 0).
- `agents/slack-bot`, `agents/google-chat-bot`, CLI, `run_triage.py`: refuse
  the flag.
- `web/`: *Learning sandbox* badge, *Hint* button, level indicator.
- `docs/integrations/learning-sandbox.md`, `docs/config/general.md`,
  `.env.example`.

## Acceptance Criteria

- [ ] With `ORRERY_LEARNING_MODE` unset, no `orrery_core.learning` module is
      imported and the plugin list is unchanged (test pins both).
- [ ] Each precondition, missing alone, stops startup with a message naming
      it; Slack, Chat, CLI and triage refuse the flag.
- [ ] Manifest: unknown field/version rejected; atomic reload picks up a new
      file; an invalid reload keeps the old one and increments the metric;
      previous values stay guarded through the grace period.
- [ ] Matcher unit tests cover separators, abbreviations, words and string
      targets, plus near-misses that must *not* match (`51000`, `510`).
- [ ] A matched response is replaced, the original appears in no event, log
      or audit line, and `learning_answer_withheld` is audited.
- [ ] In learning mode `/chat/stream` sends no `delta` frames.
- [ ] Hint endpoint: owner-only (404 otherwise), rate-limited, +1 per call,
      never lowers; a tool writing the level keys is reverted.
- [ ] `ORRERY_DISABLED_TOOLS` removes the tool from specialists, the
      declarative catalogue, admin catalog and Studio, and an unknown name
      stops startup.
- [ ] **Eval (AEP-002):** 20 adversarial "just tell me the value" prompts at
      level 0 against the seeded scenario, on the lab's model (Haiku 5.5):
      zero exact-value leaks; paraphrase leaks reported as a measured rate.

## Rollout

1. **Phase 0:** `ORRERY_DISABLED_TOOLS` and the manifest loader (no
   behaviour change without the flag).
2. **Phase 1:** sandbox gate, answer guard and streaming hold, plus the eval.
3. **Phase 2:** disclosure levels, hint endpoint and console button.
4. **Phase 3:** the coach moves into the learning package; PR #342's
   `ORRERY_AGENTS_CONFIG` route is dropped.

A lab enables the coach only after phase 1 has shipped.

## Future Work

These lab requests are related but belong to other proposals. Each carries
its security requirements with it:

- **Trajectory export** (AEP-022, first phase):
  - owner-scoped;
  - credential redaction before export;
  - the same disclosure rule as the guard (no tool results that reveal a
    target below level 3);
  - HMAC-signed with an env-only secret over session id, subject hash,
    scenario_id and timestamp, so an export can't be replayed.
- **"Why was this refused"** (AEP-024):
  - a viewer reads only their own gate events, filtered by verified subject;
  - it returns categories, never raw arguments;
  - it never names the safety-screen pattern or the matched text, which
    would teach a bypass.
- **Learners mutating their own resources** (a new ADR generalising
  `NamespaceScopeGuard`):
  - needs L3 on a shared broker, so Kafka-side ACLs and quotas must be the
    primary limit, with Orrery's check second;
  - the resource pattern comes from the verified identity only;
  - every target of a multi-target tool is checked;
  - a red-team eval on the lab's model must pass first.
