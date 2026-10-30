# AEP-032: Learning Sandbox Mode for Practice Labs

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">in-progress</span> (phases 0–1 built: `ORRERY_DISABLED_TOOLS`, the manifest loader, the sandbox gate and the answer guard) |
| **Priority** | <span class="badge badge--grey">P3</span> |
| **Effort** | Medium (5-7 days, in four phases) |
| **Impact** | Medium (practice labs only; no effect on any other deployment) |
| **Dependencies** | AEP-009 (streaming), AEP-002 (evals), ADR-005 (specialist selection), ADR-006 (caller principals) |

!!! warning "A sandbox mode, not an agent feature"
    Everything here is **off by default** and lives in its own package,
    `orrery_core.learning`, which is imported only when
    `ORRERY_LEARNING_MODE=true`. With the flag on, Orrery refuses to start
    unless the process is the HTTP server of a read-only, authenticated,
    single-replica practice sandbox. A production deployment never loads it,
    and with the flag off nothing in this proposal changes how Orrery
    behaves. The only general-purpose piece, per-tool narrowing (phase 0),
    can only take tools away, and with it unset the agent tree is identical
    to today's.

## Gap Analysis

Orrery is used as the subject of hands-on labs. A learner gets a seeded fault
(a Kafka consumer group with lag, an alert firing) and is asked to diagnose
it. Three things make that a poor learning exercise today:

- **The agent answers the exercise.** Orrery is built to lead with the
  verdict and quote exact values (`OPERATING_PRINCIPLES`). Asked "what is the
  lag?", it reads the live system and says the number, so the lab grades how
  well someone prompts an agent, not how they diagnose.
- **A prompt cannot fix it.** A coaching instruction ("don't give the answer
  away") is a request to a model that has just read the answer from a tool.
  A determined "just tell me the number" gets it. The coordinator can also
  route around the coach to a specialist that has no such instruction, or
  run a triage sweep whose report shows up in its own panel.
- **Help is invisible.** Nothing records how much help a learner needed, so a
  lab cannot weigh a solution reached alone against one reached after the
  agent spelled it out.

What is missing is a server-side control that knows the exercise's answers
and keeps them out of everything the learner sees until they ask for them,
plus a record of every step of help.

## Non-goals

- **Not an exam-proof control.** The learner can still reason from the
  evidence Orrery shows them, which is the point. The guard stops Orrery
  from *stating* an answer. It does not stop a learner from working it out.
- **Not a security boundary.** It protects exercise answers, not secrets.
  The security boundary stays where it is: auth, RBAC, autonomy and the
  gates (ADR-001, ADR-006).
- **Not a tutoring product.** No curricula, scoring or progress tracking
  inside Orrery: the lab platform owns tasks and grading. Orrery only
  withholds answers, hands out hints and reports what happened.
- **No new way to change the lab.** Learning mode runs at L2. Letting
  learners mutate resources on a shared broker is a separate decision (see
  *Future Work*).

## Solution

### 1. The sandbox gate

**Refusal by default, in the builders every surface shares.** The agent
tree and the plugin list are built when modules are imported
(`orrery_assistant.agent` builds the chat root at import, and `app.py` calls
`default_plugins()` at import). That rules out a check that runs later.
The gate therefore has two parts:

- **The check.** A small core module, `orrery_core/learning_gate.py`,
  holds a process-level "activated" flag. It never imports the learning
  package. It is called by `create_agent()`, `default_plugins()`,
  `build_chat_agent()` and `AgentGateway.__init__`. When
  `ORRERY_LEARNING_MODE` is set and the process was not activated, each of
  them raises.
  - Every agent is built by `create_agent()`, so every surface goes
    through at least one of them, including a specialist run on its own
    (`adk web` on `agents/kafka-health`). That covers the Slack and Google
    Chat bots, `adk web` (`make run-dev`, whose module-level `App` calls
    `default_plugins()`), the CLI, `run_persistent.py`, batch triage,
    `agents/ops-journal/run_persistent.py`, `scripts/demo/scripted_server.py`
    and any future entry point.
  - So all of them refuse the flag without any code of their own.
- **The activation.** `orrery_assistant/app.py`, the HTTP server's module
  (`uvicorn orrery_assistant.app:api`), calls
  `learning_gate.activate_http_server()` as its **first statement**, before
  it imports the agent module. Activation runs the preconditions below and
  then imports `orrery_core.learning`. Because it happens before
  construction, the builders that run next see an activated process:
  `AgentGateway` adds the learning plugins when it builds its Runner, and
  `build_chat_agent()` adds the coach (phase 3). A specialist's own `.env`
  is loaded after activation and may override the environment, so
  `create_app()` checks the preconditions again, together with the resolved
  server config, before it serves.
  - This only works if importing the package does not build the tree
    first. Today `orrery_assistant/__init__.py` is
    `from . import agent as agent`, and uvicorn imports the package before
    `app`. That line is removed.
  - `adk web` and `adk run` still work: when the package has no
    `root_agent`, ADK's loader imports `orrery_assistant.agent` itself.
  - Invariant, pinned by a test: nothing in the `orrery_assistant` package
    imports `agent` at package level.
  - Activation must never move into `__init__.py`. The bots import the
    package too, so every importer would activate, which defeats the gate.
  - `scripted_server.py` also calls `create_app`, but it never activates,
    so its own `default_plugins()` call refuses the flag. A demo is not a
    sandbox.

A test imports each entry point with the flag set, each in a **fresh
interpreter** (a process that already imported `orrery_assistant` would
hide the ordering bug), and expects every one to refuse except
`orrery_assistant.app`.

Within the HTTP server, `ORRERY_LEARNING_MODE=true` is refused at startup
(a `ValueError` naming the condition that is missing, the same pattern as
`studio/config.py`) unless **all** of these hold:

| Condition | Why |
|-----------|-----|
| `AUTH_ENABLED=true` | hints and answers are per person, and an anonymous caller has no identity to scope them to |
| `ORRERY_HUMAN_CLIENTS` set | only verified humans are learners (ADR-006); without it every token counts as human |
| `ORRERY_AUTONOMY_LEVEL=L2` | the sandbox is read-only; a learner-facing deployment that can mutate is not a sandbox |
| `ORRERY_SCENARIO_MANIFEST` set and valid | without it the guard has nothing to protect, and an unguarded "learning mode" is worse than none |
| `ORRERY_A2A_ENABLED` off | machine sessions (`a2a-`) sit outside the learner's sessions and level, and an `input-required` task carries a `pending_action` data part; refusing A2A keeps every learning turn a human console turn |
| `ORRERY_STUDIO_ENABLED` off | a Studio run builds its own gateway and plugins, so its output is not under the guard; run Studio as a separate deployment (AEP-031 already recommends that for public demos) |
| `ORRERY_ADMIN_EDITING` off | a catalog edit rebuilds the tree; keeping it off removes one way to change what learners talk to |
| `ORRERY_MULTI_REPLICA` off, and a single worker (`WEB_CONCURRENCY` unset or 1) | the manifest's reload state and the hint store are per process; `uvicorn --workers N` would split them inside one pod |
| `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` false (activation sets it, since ADK reads unset as on), and `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` unset or `NO_CONTENT` | ADK traces the original model response *before* any after-model callback runs, so captured content would export withheld text |
| `google_adk` and LiteLLM loggers above DEBUG | ADK logs every streamed model response at DEBUG |

When the mode is active, `GET /healthz` reports `"mode": "learning"` and
the loaded `scenario_id` (the id is not secret; the values are). The console
shows a persistent *Learning sandbox* badge, so nobody mistakes a lab for an
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

- **Strict schema.** Pydantic with
  `ConfigDict(extra="forbid", hide_input_in_errors=True)`, as
  `agent/config.py` already does. An unknown field or `schema_version` is an
  error, so a typo fails loudly instead of guarding nothing. A validation
  failure is logged from `e.errors(include_input=False)`, as field
  locations and error types only, so an error line never repeats a value.
- **The manifest cannot switch off its own guard.**
  - `targets` must hold at least one entry.
  - `default_disclosure` must be 0, 1 or 2; level 3 is reachable only
    through hints.
- **Values a matcher can work with.** `value` is a number or a string.
  - A number must be at least 100 in absolute value. A string needs at
    least four characters.
  - A value like `3` (replicas) or `1` (a partition) would match almost
    every reply ("step 1", "level 3"), so it is refused at load.
  - A manifest where any value appears inside a `descriptor`, a target
    `id`, a `unit` or the `scenario_id` is refused as well: the guard's
    nudge is built from descriptors, and ids reach audit lines and
    `/healthz`.
  - Parsing is pydantic's strict JSON mode: exact types (no `true` for `1`,
    no `"2"` for `2`), timezone-aware `seeded_at`, no NaN or infinity, and
    numbers below 2**53. Only a regular file is read.
  - The lab never ships a list of formats. The guard derives them (§4).
- **Narrow-only by construction.** The schema has no field that can raise
  autonomy, grant a role or add a tool. A later `hide_tools` field may only
  remove tools.
- **Writing and loading.**
  - The lab's seeder writes the file atomically (a temp file in the same
    directory, then `rename`) onto a volume mounted **read-only** into
    Orrery.
  - The seeder writes the manifest **before** it seeds the fault.
  - Orrery checks the file's `stat()` (inode, mtime and size) synchronously
    on every guard call and reloads on any change, so no polling interval
    leaves new values unguarded.
  - The lab opens the exercise only once `/healthz` reports the new
    `scenario_id`.
- **Reload failures.**
  - Invalid at startup: startup fails.
  - Invalid on reload: the last valid manifest stays in force, an ERROR is
    logged (no values, see above) and `orrery_learning_manifest_errors_total`
    increments.
- **Grace.** Any value that leaves the manifest, whether the
  `scenario_id` changed or the same id was re-seeded with new values,
  stays guarded for `ORRERY_LEARNING_GRACE_SECONDS` (default 3600). A
  learner mid-attempt is therefore not handed the old answer.
- **Confidentiality, stated precisely.**
  - Orrery's learning code never writes a target value anywhere: no
    prompt, session state, trace attribute, log line, audit entry or
    export. Audit lines carry `scenario_id` and the target `id` only.
  - The values do exist in **tool traffic**: a lag tool returns the lag.
    Tool results and arguments can reach logs (AuditPlugin in `full`
    mode), and model content can reach traces if an operator enables
    capture. So in a lab, learners have no access to Orrery's logs, audit
    stream, Tempo or Grafana.
  - The manifest file itself must not be reachable through any tool that
    is enabled: not a ConfigMap readable by `get_configmap`, and not seeder
    container logs readable by the docker specialist.

### 3. Disclosure levels and hints

Each learner has a disclosure level from 0 to 3 **per scenario**, starting
at the manifest's `default_disclosure`:

| Level | What the agent may say |
|-------|------------------------|
| 0 | questions only: which signal to look at |
| 1 | where to look (a topic, a group, an alert) |
| 2 | what to compare, without the conclusion |
| 3 | the answer, with its evidence; target values are no longer withheld |

- **Bound to the scenario.**
  - The level is stored as `(scenario_id, level)`. A level recorded for any
    other scenario reads as `default_disclosure`, so reaching level 3 on
    scenario A never unlocks scenario B.
  - Help is counted per `(subject, scenario_id)`: opening a new chat does
    not reset the level.
- **Only a person raises it.**
  - The console gets a *Hint* button that calls `POST /learning/hint`.
  - There is deliberately **no model-callable tool**: a learner could talk
    the model into calling it.
  - The endpoint needs a verified **human** principal (`principal_kind ==
    HUMAN`), not a delegated or service one.
  - It is rate-limited per subject, raises the level by exactly one step,
    and never lowers it.
  - It returns the new level in its response.
- **Out of band, never mid-turn.**
  - The endpoint writes the learning package's own per-process store,
    keyed by subject and scenario. It never appends to a session.
  - Appending while a turn runs would make that turn's next event fail
    (`StaleSessionError` in the database session service).
  - At the start of each turn, `inbound()` copies the current level into
    the turn's `state_delta` with a server-set marker, the same way the
    auth context travels.
  - Both keys are defined in core and added to `IdentityStateGuardPlugin`'s
    protected set, so no tool can change them, and core never imports the
    learning package to know them. A bare value without the marker is
    ignored and read as level 0.
- **Every change is recorded.**
  - A `learning_hint` audit entry: subject, scenario_id, old and new level.
  - `orrery_learning_hints_total{level}`.
- **A restart fails safe.** The hint store is per process. After a
  restart every learner reads as `default_disclosure` again, which withholds
  more, never less. The durable record of help is the `learning_hint` audit
  stream.
- **The model sees the level, never the values.** A learning-package
  `before_model_callback` appends one line to the **root's** system
  instruction: the current level and what it permits. Core's
  `identity_aware_instruction`, which wraps every agent in every
  deployment, is left alone.

### 4. The answer guard

`AnswerGuardPlugin` is an `after_model_callback`, registered only in an
activated process, by `AgentGateway` when it builds its Runner, **first among
after-model plugins** (pinned by a test). ADK's plugin chain stops at the first
callback that returns a value, so the guard **edits the response in place and
returns `None`**: a returned replacement would end the chain, and
`TokenBudgetPlugin` settles a call's tokens in that same chain. ADK builds the
event from the same object, so the edit is what gets stored and sent.

**What it checks.**

- It checks a response when `callback_context.agent_name` equals the name
  of the gateway's current root agent. It reads that name per turn, so it
  follows `replace_root_agent()`, and agent names are unique across the
  catalogue.
- Every specialist runs in an `AgentTool` sub-runner that inherits the
  plugins. Inside that sub-runner the specialist is the root of its own
  invocation, so "the root" means the gateway's root, not the
  invocation's.
- It checks **every** non-partial root response that carries non-thought
  text, not only the last one. The gateway's reply joins all of them, and
  a root model often writes text before a tool call.
- Streamed partials are skipped: they are never forwarded (see the
  streaming hold below).

**How it matches.**

- Numbers:
  - digit sequences on token boundaries, with or without separators
    (`5100`, `5,100`, `5 100`, `5.100`);
  - `k`/`M` abbreviations within the value's rounding;
  - the number in words.
- Strings: a case-insensitive substring.
- The matcher is deterministic and unit-tested, including near-misses that
  must not match.

**What happens on a match, while the learner's level is below 3.**

- The text parts are replaced with a fixed nudge ("I can't give that
  value yet. Here's what to look at next: …"), built from the target's
  `descriptor` and the current level. Thought parts are dropped.
  `function_call` parts are kept, so a mixed text-and-tool-call response
  still runs its tools.
- The nudge itself is run through the matcher before it is used.
- One nudge per turn: the gateway joins every root response into one
  reply, so a later match in the same turn keeps only its tool calls, or
  becomes a short fixed line. It never leaves an empty text part, which
  Gemini rejects in history.
- The original text is never stored or logged.
- A `learning_answer_withheld` audit entry records the target id and
  scenario_id, never the value.
- ADK builds the stored session event and the gateway reply from the
  altered response, so the transcript (`GET /session/{id}`) holds the
  nudge, not the original.

**Route-independent.** The guard sits on the root's text, so it holds
whether the coordinator used the coach, a specialist, memory recall or an
MCP tool. Their text reaches the learner only through the root.

**Invariant for short-circuits.**

- A `before_model_callback` that answers instead of the model
  (`TokenBudgetPlugin`, `AttachmentPlugin`) skips every after-model
  callback, including this one.
- Those answers are fixed texts today. The proposal makes that an
  invariant, enforced by a test: no before-model answerer may emit text
  derived from a model or a tool.

**Other learner-facing surfaces.** Two endpoints show text the model wrote,
outside the root's reply. In learning mode they are narrowed:

- `GET /session/{id}/triage` returns `incident_severity` only. The
  `triage_report` is written by the triage summarizer and forwarded into the
  root session by `AgentTool`, so it can state a value.
- `GET /session/{id}/activity` returns operation, agent and status only,
  without the argument summary. Tool arguments are written by the model
  (`AgentTool` requests, `record_triage_verdict(report=…)`, ops-journal
  `save_note(text=…)`), so they can carry a computed value.

**Streaming hold.**

- `POST /chat/stream` normally sends `delta` frames as the model writes,
  and text already sent cannot be retracted.
- In learning mode the SSE layer drops `delta` frames. It sends the checked
  `message` frame only, which already carries the full reply.
- `progress` frames (tool names and outcomes, never arguments or results)
  still flow, so the console still shows the agent working.

**Residual risks, stated:**

- a paraphrased approximation ("about five thousand");
- other encodings: reversed digits, digits spelled one by one, hex, an
  arithmetic expression ("5099+1");
- a value split across two root responses.

The eval measures these. The proposal does not promise to block them.

### 5. The coach

The declarative `lab_coach` agent (read-only kafka and observability tools,
a four-step hint ladder; first drafted in PR #342) becomes part of the
learning package. `build_chat_agent()` adds it **only in an activated
process** (§1), so admin catalog rebuilds keep it too (and admin editing is
off in learning mode anyway). Its name, `lab_coach`, joins
`RESERVED_AGENT_NAMES`, so no declarative agent can take it. `ORRERY_AGENTS_CONFIG` is not the way to load it.

Its ladder follows the learner's disclosure level instead of a marker in its
own replies. It is the teaching layer; the guard is the safety layer. The
coach is never relied on to withhold anything.

### 6. Per-tool narrowing (phase 0, general)

`ORRERY_DISABLED_TOOLS` (comma-separated, qualified names like
`observability.get_alert_groups`) removes tools from their specialists before the
tree is built. It extends ADR-005 and follows its rules:

- **It only removes.** There is no allowlist that could add a tool back.
- **Unknown names fail.** An unknown or ambiguous name stops startup.
- **Unset changes nothing.** With the variable unset, the tool tree and
  the plugin list are identical to today's (pinned by a test).
- **Everything that builds from tools sees the removal.** That covers the
  declarative catalogue, admin catalog edits and Studio's tool list.
- **References to a removed tool fail at startup.** A specialist's
  `HealthCheck` (the triage sweep) or `SpecialistSpec.remediation` playbook
  that names one stops startup, rather than silently shrinking the sweep.
- **It is enforced at runtime too.** `GuardrailsPlugin` refuses a call to
  a removed tool. It matches on the tool's **function identity** resolved
  at build time, not its bare name: bare names collide across specialists,
  and a declarative agent holding `k8s.get_events` has a different agent
  name.
- **Refusals don't open circuits.** The refusal status joins
  `_NEUTRAL_STATUSES`, so a refused call never counts as a failure.
- **MCP is out of scope.** MCP tools keep their own required allowlist
  (`tools:` in `ORRERY_MCP_CONFIG`). Naming an MCP tool here is an unknown
  name and stops startup.

It is not learning-specific (any deployment can narrow itself), which is why
it is the one piece outside `orrery_core.learning`. A lab uses it to make a
learner compute a value instead of reading it from a tool, within one limit
found while building it: a tool a health check uses stays, because the
triage sweep would still call it. `kafka.get_consumer_lag` feeds the Kafka
health check, so a lab cannot hide it; the answer guard is what withholds the
lag.

## Design decisions

- **The lab states the answers; Orrery doesn't infer them.**
  - A target like "total lag" is a sum the model computes, so no tool
    returns it verbatim.
  - Inferring answers from tool output would be guesswork, and a wrong
    guess leaks.
  - The seeder knows the truth, so it writes it down.
- **The context comes from the deployment, not from a caller.** A
  per-session context set by a trusted service would mean a service
  principal writing into a human's session. ADR-006 forbids that
  (`owns_session`), and opening it up for labs would weaken the boundary
  every other surface relies on. One scenario per deployment fits how labs
  run.
- **Fail closed everywhere.**
  - Every entry point except the HTTP server refuses the flag.
  - A missing precondition refuses startup.
  - A bad manifest keeps the last good one.
  - A missing or unmarked level reads as 0.
- **A separate package.** `orrery_core.learning` is imported only behind
  the flag, mirroring `studio/` and `a2a_server.py`. In a normal deployment
  no learning code runs and no learning plugin is registered. The protected
  state-key names live in core, so core never needs to import the package.

## Threat Model

| Threat | Control |
|--------|---------|
| Learner talks the model into stating a value | Answer guard on every root text response; adversarial eval |
| Coordinator routes around the coach, or the learner asks for a triage sweep | Guard is route-independent; `/triage` narrowed to severity |
| A value appears in model-written tool arguments | `/activity` drops the argument summary in learning mode |
| Streamed text reveals the value before the guard runs | `delta` frames dropped in learning mode |
| A mixed text-and-tool-call response leaks text, or loses its tool call | Text parts replaced, function calls kept |
| The guard's own nudge discloses a value | Descriptors may not contain values; nudge re-checked |
| A plugin earlier in the chain returns first | Guard registered first among after-model plugins (pinned) |
| A before-model answerer emits derived text | Invariant pinned by a test: fixed texts only |
| A tool or injected text raises the disclosure level | Level stamped by `inbound()` with a server-set marker; keys protected by `IdentityStateGuardPlugin` |
| Level reached on one scenario unlocks the next | Level bound to `scenario_id` |
| Learner raises another learner's level, or a machine principal does | Endpoint keyed by the verified human subject; non-human principals refused |
| Hint endpoint abused for load | Per-subject rate limit; one step per call |
| New values unguarded during a re-seed | Manifest written before seeding; reload checked on every guard call; lab waits for `/healthz` to report the new id |
| Values leak through Orrery's own logs, errors or audit | Learning code never writes values; `hide_input_in_errors`; audit carries ids only |
| Values leak through tool traffic in logs and traces | Span content capture off and DEBUG off as preconditions; learners have no access to logs, audit, Tempo or Grafana |
| Learner reads the manifest through a tool | The manifest must not be reachable by any enabled tool (deployment rule) |
| Manifest disables its own guard or widens access | At least one target; `default_disclosure` ≤ 2; no widening fields; read-only volume |
| Manifest broken mid-session | Last valid manifest stays in force; metric plus an ERROR log |
| Unguarded paths: Studio, A2A, `adk web`, bots, CLI | Studio and A2A are preconditions; the shared builders refuse the flag unless `orrery_assistant.app` activated the process |
| Learning mode set on an operational deployment | No manifest ⇒ startup refused. Even if it started, the effect is answers being *withheld*, not exposed |

Accepted residual risks: the encodings and the split across responses
listed in §4, and a learner reasoning from raw evidence the agent showed
them. Both are inherent to a diagnostic exercise.

## Affected Files

- `core/orrery_core/learning/` (new, imported only behind the flag):
  - `__init__.py`: wiring called by the activated builders
  - `config.py`: flag and preconditions
  - `manifest.py`: schema, stat-checked reload, grace
  - `guard.py`: matcher and `AnswerGuardPlugin`
  - `hints.py`: level store, `POST /learning/hint`, root instruction line
  - `coach.yaml`
- `core/orrery_core/learning_gate.py` (new, always importable, no learning
  code): the activated flag, `activate_http_server()`, and the check called
  by `create_agent()`, `default_plugins()`, `build_chat_agent()` and
  `AgentGateway.__init__`.
- `agents/orrery-assistant/orrery_assistant/app.py`: activate as the first
  statement.
- `agents/orrery-assistant/orrery_assistant/__init__.py`: drop the
  package-level `agent` import.
- `core/orrery_core/agent/base.py`: the activation check in
  `create_agent()`.
- `core/orrery_core/plugins/identity_guard_plugin.py` (and the core module
  that defines state keys): the level keys.
- `core/orrery_core/serving/server.py`: narrow `/triage` and `/activity`,
  report the mode in `/healthz`, and have `inbound()` stamp the level.
- `core/orrery_core/serving/sse.py`: drop `delta` frames in learning mode.
- `core/orrery_core/serving/gateway.py`: the activation check in
  `AgentGateway.__init__`.
- `core/orrery_core/registry.py`, `agent/declarative.py`,
  `security/guardrails.py`, `reliability/resilience.py`:
  `ORRERY_DISABLED_TOOLS` (phase 0).
- `agents/orrery-assistant/orrery_assistant/agent.py`: `build_chat_agent()`
  adds the coach when activated; `lab_coach` joins `RESERVED_AGENT_NAMES`.
- `web/`: *Learning sandbox* badge, *Hint* button, level indicator.
- `docs/integrations/learning-sandbox.md`, `docs/config/general.md`,
  `.env.example`.

## Acceptance Criteria

**Off by default**

- [x] With `ORRERY_LEARNING_MODE` unset, no `orrery_core.learning` module is
      imported, and the plugin list is unchanged (a test pins both).
- [x] With `ORRERY_DISABLED_TOOLS` unset, the agent tree is identical to
      today's.

**The sandbox gate**

- [x] With the flag set, importing each entry point raises, except
      `orrery_assistant.app` (a test walks the entry points, each in a
      fresh interpreter, standalone specialists included).
- [x] Nothing in `orrery_assistant` imports `agent` at package level.
- [x] In-process learning tests reset the activated flag in a fixture, so
      activation never leaks into the rest of the workspace's single
      pytest run.
- [x] Each precondition, missing on its own, stops startup with a message
      naming it.

**The manifest**

- [x] An unknown field or version is rejected.
- [x] `targets: []` and `default_disclosure: 3` are rejected, and so are
      short values and values that appear in a descriptor.
- [x] An invalid manifest's error log contains no value.
- [x] A file replaced atomically is picked up on the next guard call.
- [x] An invalid reload keeps the old manifest and increments the metric.
- [x] Removed values stay guarded through the grace period.

**The answer guard**

- [x] Matcher unit tests cover separators, abbreviations, words and string
      targets, plus near-misses that must *not* match (`51000`, `510`,
      `5100` inside a longer token).
- [x] Specialist (`AgentTool`) responses are not rewritten. Every root text
      response is checked. A mixed response keeps its function calls.
- [x] A matched response is replaced, and the original appears in no event,
      transcript, log or audit line. `learning_answer_withheld` is audited.
- [x] A seeded value in a triage report or in tool arguments does not
      appear in `/triage` or `/activity` below level 3.
- [x] In learning mode, `/chat/stream` sends no `delta` frames.
- [x] The guard is first among after-model plugins, and no before-model
      answerer emits derived text (both pinned by tests).

**Hints**

- [ ] The hint endpoint refuses non-human principals.
- [ ] It is keyed by subject and rate-limited, adds +1 per call and never
      lowers the level.
- [ ] A level from another scenario reads as the default.
- [ ] A tool writing the level keys is reverted.
- [ ] A hint pressed during a running turn does not break that turn.

**Tool narrowing**

- [x] `ORRERY_DISABLED_TOOLS` removes the tool from specialists, the
      declarative catalogue, the admin catalog and Studio, and is refused
      at runtime by function identity.
- [x] The refusal status is neutral for the breaker.
- [x] An unknown name, or one a `HealthCheck` or remediation playbook still
      references, stops startup.

**Eval (AEP-002)**

- [ ] The eval runs on the lab's model (Haiku 5.5) through the full plugin
      stack, as `test_k8s_red_team_eval.py` does.
- [x] It uses 20 or more adversarial prompts at level 0: "just tell me the
      value", the encodings from §4, and route-arounds ("ask the kafka
      agent directly", "run a triage sweep").
- [ ] With the guard on there are zero exact-value leaks, and paraphrase
      leaks are reported as a measured rate.
- [ ] The leak rate with the guard off is reported too, to measure the
      model-side layer.

## Rollout

1. **Phase 0:** `ORRERY_DISABLED_TOOLS` and the manifest loader (no
   behaviour change without the flag).
2. **Phase 1:** the sandbox gate, the answer guard, the surface narrowing
   and the streaming hold, plus the eval.
3. **Phase 2:** disclosure levels, the hint endpoint and the console
   button.
4. **Phase 3:** the coach moves into the learning package, and PR #342's
   `ORRERY_AGENTS_CONFIG` route is dropped.

A lab enables learning mode only after phase 1 has shipped in a release.
Because Studio must be off, a lab that also demonstrates Studio runs it as a
separate Orrery deployment.

## Future Work

These lab requests are related but belong to other proposals. Each carries
its security requirements with it:

- **Trajectory export** (AEP-022, first phase):
  - only the session's owner can fetch it;
  - credential redaction runs before export;
  - it follows the same disclosure rule as the guard: no tool results or
    model-written arguments that reveal a target below level 3;
  - it is HMAC-signed with an env-only secret over the session id, a hash
    of the subject, the scenario_id and a timestamp, so an export cannot be
    replayed.
- **"Why was this refused"** (AEP-024):
  - a viewer reads only their own gate events, filtered by verified
    subject;
  - it returns categories, never raw arguments;
  - it never names the safety-screen pattern or the matched text, which
    would teach a bypass.
- **Learners mutating their own resources** (a new ADR generalising
  `NamespaceScopeGuard`):
  - it needs L3 on a shared broker, so Kafka-side ACLs and quotas must be
    the primary limit, with Orrery's check second;
  - the resource pattern comes from the verified identity only;
  - every target of a multi-target tool is checked;
  - a red-team eval on the lab's model must pass first.
