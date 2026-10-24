# Orrery Studio (preview)

The Studio runs a **composition**: local agents, remote [A2A](a2a.md) agents
and [MCP servers](mcp.md), wired into a pattern, run apart from the live
coordinator and traced step by step. This page covers P0a and P0b of
[AEP-031](../enhancements/aep-031-orrery-studio.md): the HTTP API, the
endpoint registry, the patterns (pipeline, router, loops, parallel branches,
supervisors), per-node models, stored compositions and run history, and the
trace events, plus the P1 canvas in the web console ([The console](#the-console)).

A Studio run is **separate from** the live system, not **isolated from** it:

- it never changes the live agent tree, the catalog, chat sessions or long-term
  memory;
- its catalogue tools are the real ones, so every run is held to autonomy
  **L2** (reads only), whatever the deployment's ceiling;
- it reaches only the MCP servers and A2A agents the operator listed in the
  registry.

## Enabling it

```bash
uv sync --extra a2a --extra mcp                 # source installs; the image has both
export ORRERY_STUDIO_ENABLED=true
export ORRERY_STUDIO_REGISTRY=/etc/orrery/studio-registry.yaml
export ORRERY_HUMAN_CLIENTS=orrery-console      # required, see below
make run-api
```

The Studio is for **signed-in human admins**. Every `/studio/*` route needs the
admin role and a human principal: a token whose client (`azp`) is listed in
`ORRERY_HUMAN_CLIENTS` ([ADR-006](../config/security.md#people-agents-and-services-adr-006)).
A service token with the admin role gets 403. With `ORRERY_HUMAN_CLIENTS`
unset every token counts as a person, so startup **fails** when the Studio is
on and it is unset, and when `AUTH_ENABLED` is off.

| Variable | Default | Meaning |
|---|---|---|
| `ORRERY_STUDIO_ENABLED` | `false` | Mount `/studio/*`. |
| `ORRERY_STUDIO_REGISTRY` | — | The registry YAML (below). Unset: an empty registry (catalogue tools only). Set but missing or invalid: startup fails. |
| `ORRERY_STUDIO_CAPTURE_CONTENT` | `false` | Put redacted text into trace events (below). |
| `ORRERY_STUDIO_MAX_TOKENS` | `200000` | Ceiling for a spec's `budgets.max_tokens`, and the default when it sets none. |
| `ORRERY_STUDIO_MAX_TOOL_CALLS` | `60` | Ceiling and default for `budgets.max_tool_calls`. |
| `ORRERY_STUDIO_MAX_DELEGATIONS` | `20` | Ceiling and default for `budgets.max_delegations`. |
| `ORRERY_STUDIO_MAX_REMOTE_CALLS` | `20` | Ceiling and default for `budgets.max_remote_calls`. |
| `ORRERY_STUDIO_MAX_TIMEOUT_SECONDS` | `300` | Ceiling and default for `budgets.timeout_s`. |
| `ORRERY_STUDIO_MAX_RUNS` | `100` | Runs kept in memory; past it the oldest finished ones go. |
| `ORRERY_STUDIO_MAX_RUNS_PER_USER` | `20` | Finished runs kept per person; past it that person's oldest go. |
| `ORRERY_STUDIO_MAX_CONTENT_BYTES_PER_RUN` | `262144` | Captured `content` one run may hold; past it events carry none. |
| `ORRERY_STUDIO_RUN_RETENTION_SECONDS` | `3600` | How long the replica that ran a run keeps it in memory; after that it is read from the store. |
| `ORRERY_STUDIO_RUN_RETENTION_DAYS` | `14` | Stored runs and their events are purged after this many days (checked hourly). |
| `ORRERY_STUDIO_MODELS` | — | The models a node may name in `model` (comma-separated, e.g. `gemini-2.5-flash,anthropic/claude-sonnet-4-5`). Unset: nodes use the deployment's model and naming one is refused. Each is built at startup: a bad name, a LiteLLM model without `litellm` installed, or a missing provider key (`GOOGLE_API_KEY` or Vertex AI for Gemini; LiteLLM's own check otherwise) fails startup. |
| `ORRERY_STUDIO_MAX_RUNNING_PER_USER` | `2` | Runs one person may have in flight; more is a 429. |
| `ORRERY_STUDIO_MAX_RUNNING` | `8` | Runs in flight on the replica. |
| `ORRERY_STUDIO_RATE_LIMIT` | `10/minute` | `POST /studio/runs` per person. |
| `ORRERY_A2A_PRIVATE_ALLOW` | — | Hosts and CIDRs a `private_network` entry may reach (comma-separated). |
| `ORRERY_A2A_CA_FILE` | — | One extra CA bundle trusted for outbound A2A (an internal CA). |
| `ORRERY_STUDIO_SECRET_*` | — | Bearer tokens registry entries refer to by name. |
| `ORRERY_REDACT_IPS` | `false` | Also redact IP addresses in captured `content`, as it does in logs. |

`ORRERY_DAILY_TOKEN_BUDGET_PER_USER` applies to Studio runs too: every run
charges the same per-process ledger as chat.

**Storage.** With `DATABASE_URL` set to PostgreSQL, compositions and runs go
to four new tables (`studio_compositions`, `studio_composition_versions`,
`studio_runs`, `studio_run_events`), created at startup if missing; no
existing table changes. An unreachable database fails startup, unless
`ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1` (the session store's rule). Without
PostgreSQL both stay in memory, on each replica, bounded by
`ORRERY_STUDIO_MAX_RUNS`.

## The registry

A composition can reach only what the registry lists, by id. It is deployment
config, read at startup like `ORRERY_MCP_CONFIG`; nothing in a request can add
to it.

```yaml
mcp:
  - id: tirraflow                     # lowercase; tools are mcp_tirraflow_<tool>
    url: https://tirraflow.com/mcp
    transport: streamable_http        # or sse; never stdio
    tools: [search_articles, get_article]
    read_only: [search_articles, get_article]

a2a:
  - id: demo-writer                   # tools are a2a_demo_writer_<skill>
    card_url: https://demo-writer.studio.internal:8443/.well-known/agent-card.json
    card_sha256: "029d85…"            # optional: pin from config (see Pinning)
    skills: [draft]                   # the card's skills this entry may use
    effect: read_only                 # `acting` is refused until P0c
    private_network: true             # and the host must be in ORRERY_A2A_PRIVATE_ALLOW
    auth: { bearer_env: ORRERY_STUDIO_SECRET_DEMO_WRITER }
```

**MCP entries** are the same schema as `ORRERY_MCP_CONFIG` servers
([MCP servers](mcp.md#configuration)), with `id` for `name` and
`private_network` for `allow_private_network`, minus what a composition must
not carry: stdio servers (`command`, `args`, `env_from`), headers from env,
per-user token exchange (`auth`), file paths (`ca_file`, client certificates)
and `allow_insecure_http`. Plain http is accepted on loopback only. Only tools
listed in `read_only` can be used in a P0a composition.

**A2A entries** follow [ADR-008](../adr/008-consuming-remote-a2a-agents.md):

| Key | Default | Meaning |
|---|---|---|
| `id` | — | `^[a-z][a-z0-9_-]{1,40}$`. |
| `card_url` | — | https (plain http only loopback to loopback). No credentials in the URL. |
| `card_sha256` | — | The pinned digest. Unset: pinned on first use and logged. |
| `skills` | — | At least one. A skill the card does not declare is refused when the card is fetched. |
| `effect` | `read_only` | `acting` is refused with "not supported yet (P0c)". |
| `private_network` | `false` | Allow a private address, for a host or address listed in `ORRERY_A2A_PRIVATE_ALLOW`. |
| `service_origins` | `[]` | Extra origins the card's RPC interface may point to (default: the card's own origin). |
| `auth.bearer_env` | — | An env var whose name starts with `ORRERY_STUDIO_SECRET_`. Sent only to the entry's origins (the card's own, plus `service_origins`); a redirect or another origin never gets it. Unset at startup: startup fails. |
| `connect_timeout_s` / `read_timeout_s` / `total_timeout_s` | 5 / 60 / 120 | Per call. |
| `max_card_bytes` | 65536 | Card size, checked while it streams. |
| `max_reply_bytes` | 524288 | Response size, checked while it streams. |
| `max_reply_chars` | 16000 | Reply text handed to the model; past it the reply is cut and `truncated: true`. |
| `max_parts` | 32 | Parts in a reply; more refuses it. |
| `card_refresh_s` | 300 | How often the card is fetched again and re-checked against the pin. |

**Pinning.** The card's digest is the SHA-256 of the RFC 8785 canonical JSON of
its trust fields: interfaces (URLs, bindings, protocol versions), security
schemes and requirements, and skills (without their examples). A changed
description is fine; a changed endpoint, auth scheme or skill list disables the
entry until an admin re-pins it. `GET /studio/registry?resolve=true` fetches
each card and shows its digest: review it, then set `card_sha256`.

**The network guard** applies to every request, card and RPC, on every
connect: public addresses only, unless the entry sets `private_network` and its
host (or the address it resolves to) is in `ORRERY_A2A_PRIVATE_ALLOW`.
Link-local and metadata addresses are always refused. Loopback hosts may reach
loopback; a host on the allowlist never may, whatever it resolves to. Redirects are never followed and proxy variables are ignored. A
`private_network` entry whose host is a literal address not in the allowlist,
or any `private_network` entry while the allowlist is empty, fails startup.

**Compressed responses are refused.** Every request asks for
`Accept-Encoding: identity`, and a reply that comes back compressed anyway is
refused before a byte is decoded, so the size caps hold for what is read. The
same applies to Studio MCP entries.

**Other bindings.** A card may also list gRPC or HTTP+JSON interfaces. Orrery
calls only the JSON-RPC ones, so only those must pass the origin and https
checks; the others are not checked but stay in the pin, so a change to them
still disables the entry.

**Trust and pins for MCP entries.** Registry MCP servers use the same
`ORRERY_A2A_CA_FILE` as A2A entries. Their tool pins (each tool's description
and schema, first seen) are kept per process, across runs, so a server that
changes a tool between runs is caught.

## Compositions

A composition is a JSON document (`spec_version: 1`): nodes, edges between
them, a `pattern` (a label for the console; the edges decide what runs) and
budgets. Each node gets the previous node's answer as its input (the first
gets the run's input), and the run's output is the last answer.

```json
{
  "spec_version": 1,
  "name": "release-notes",
  "pattern": "critic_loop",
  "budgets": { "max_tokens": 120000, "max_tool_calls": 20, "max_remote_calls": 4, "timeout_s": 240 },
  "nodes": [
    { "id": "research", "kind": "llm_agent",
      "instruction": "Search Tirraflow for the topic and answer with short notes.",
      "tools": ["mcp:tirraflow.search_articles", "mcp:tirraflow.get_article"] },
    { "id": "writer", "kind": "llm_agent",
      "instruction": "Send your input to the demo writer's draft tool; answer with its draft.",
      "tools": ["a2a:demo-writer.draft"] },
    { "id": "critic", "kind": "llm_agent",
      "instruction": "Approve the draft, or reject it with what to change.",
      "output": { "labels": ["approved", "rejected"] } }
  ],
  "edges": [
    { "from": "research", "to": "writer" },
    { "from": "writer", "to": "critic" },
    { "from": "critic", "to": "writer", "when": "rejected", "max_iterations": 3 }
  ]
}
```

| Tool reference | What the node gets |
|---|---|
| `mcp:<entry>.<tool>` | The tool from a registry MCP server, as `mcp_<entry>_<tool>`. Read-only tools only. |
| `a2a:<entry>.<skill>` | A `GuardedRemoteA2aTool` named `a2a_<entry>_<skill>` (below). |
| `catalog:<name>` | One of the deployment's own catalogue tools (`GET /studio/registry` lists them). Read-only ones only. |

`GET /studio/schema` publishes the JSON Schema. P0b kept `spec_version: 1`:
every P0a spec still validates and runs the same way.

### Patterns

| Building block | How |
|---|---|
| **Labels** | `"output": {"labels": ["a", "b"]}` on an `llm_agent` or `supervisor`: the node answers `{label, answer}` (structured output); the next node gets `answer`. |
| **Routing** | Edges out of a labelled node carry `when: "<label>"`, one edge per label. Or put a `router` node right after it, and the `when` edges on the router. A label with no edge ends the run there, with that answer (`POST /studio/validate` warns). Each choice emits `route.decision`. |
| **Loops** | A back edge with `when` and `max_iterations` (1–5). Each pass emits `loop.iteration`; at the cap the edge is not taken (`stopped: true`) and the branch ends with the answer the source was judging (in a critic loop, the last draft, not the rejection). A loop nested in another counts per entry: each time the outer edge is taken, the inner one starts from zero. A cycle without `max_iterations` is refused, and so is a loop containing a `parallel` or `join` node (`loop_around_parallel`): a join does not start over. |
| **Parallel** | A `parallel` node with 2–6 outgoing edges, each branch a chain of `llm_agent` nodes (no routing, loops or nested fan-out), all meeting at one `join`. The branches run concurrently; the join's successor gets each branch's answer as a `## <branch id>` block, in edge order. |
| **Supervisor** | A `supervisor` node lists `children` (`llm_agent` or `supervisor` ids, at most 3 levels). Each child is a tool the supervisor's model calls (an ADK `AgentTool`); each call counts as a delegation against `max_delegations`. Children have no edges; their `node.enter`/`node.exit` carry `parent`. |
| **Per-node models** | `"model": "<name>"` on an `llm_agent` or `supervisor`, from `ORRERY_STUDIO_MODELS`. No fallback chain: a failing model fails the node. Gemini's safety filters follow the node's model, not `MODEL_PROVIDER`. `run.end` totals tokens by model. |
| **Screening** | `"screen": true` on an edge runs the injection screen over the text it carries. |

The `pattern` values are `pipeline`, `router`, `critic_loop`,
`fan_out_fan_in`, `supervisor`, `debate`, `map_reduce` and
`plan_and_execute`; a spec whose edges do not fit its pattern is refused
(`pattern_mismatch`: a `pipeline` has no labels, fan-out or loops, a
`critic_loop` needs a loop, a `router` a `when` edge, `fan_out_fan_in` and
`map_reduce` a parallel node and its join, `supervisor` a supervisor,
`debate` parallel speakers or a loop). `handoff` and the `human_approval` and `remote_a2a` node
kinds parse but are refused with `not_supported_yet`.

**Templates.** `deploy/studio-demo/specs/` has one tested spec per pattern:
`pipeline.json`, `router.json`, `critic-loop.json` (the demo; it was
`release-notes.spec.json` in P0a), `fan-out-fan-in.json`, `supervisor.json`,
`debate.json`, `map-reduce.json` (a fixed fan-out: the map width is the number
of branches) and `plan-and-execute.json`. CI runs each with a stub model
(`core/tests/test_studio_patterns.py`).

### Validation

Validation runs before anything else, never calls a model, and returns every
problem at once, each with the path of the field that caused it:

```json
{"detail": {"errors": [
  {"loc": ["spec", "nodes", 1, "tools", 0],
   "msg": "remote agent 'demo-writer' is not registered for skill 'deploy'",
   "code": "unknown_tool"},
  {"loc": ["spec", "nodes", 3, "kind"],
   "msg": "node kind 'human_approval' is not supported yet (P0c)",
   "code": "not_supported_yet"}
]}}
```

It refuses: duplicate ids; edges to unknown nodes, self-loops, duplicate edges
and unreachable nodes; more than one first node; `when` on an unlabelled
source, an unknown label, two edges for one label, or a labelled node's edge
without `when`; a fan-out that is not a `parallel` node, or one with more than
6 branches (`too_many_branches`); a join that does not close its parallel's
branches, or a branch that fans out again; a loop around a parallel section
(`loop_around_parallel`); a cycle without `max_iterations` (`unbounded_cycle`), or
`max_iterations` on an edge that is not a back edge; supervisor children that
are not agents, have two parents, have edges, or nest deeper than 3
(`too_deep`); a `model` not in `ORRERY_STUDIO_MODELS` (`model_not_allowed`);
tool references that are malformed, unknown, not `read_only` or not registered
for the skill; budgets over the ceilings; unknown keys anywhere; and a
`spec_version` newer than the server reads. It also refuses, per node, two
tools that reach the model under the same name (`tool_name_collision`:
`mcp:my-x.t` and `mcp:my_x.t` are both `mcp_my_x_t`, and a catalogue alias may
match another tool's name), a tool named like a supervisor's child (`n_<id>`),
and an MCP tool whose model-facing name is over 64 characters
(`tool_name_too_long`). Two nodes may each use the same tool.

## Remote agents as tools

A remote A2A agent is always a **tool**, never a node
([ADR-008](../adr/008-consuming-remote-a2a-agents.md)). The node's model calls
`a2a_<entry>_<skill>(request)`, and only that `request` text leaves: the remote
never sees the session, earlier nodes or earlier tool results. Each call:

- uses the pinned card, after Orrery has checked its RPC targets (https, on the
  entry's origins, JSON-RPC);
- sends text only (at most 8192 characters), with `x-orrery-hop: 1`, and a
  bearer token only if the entry has one. The caller's own token is never
  forwarded;
- runs the remote exchange in a fresh in-memory session with no plugins: the
  outer tool call is the guarded one;
- returns a dict, so the safety screen, PII redaction (emails included, for
  remote replies), the output cap and the audit trail all see it:

```json
{"status": "success", "reply": "# Notes\n\n- fixed the login bug\n",
 "task_state": "completed", "task_id": "…", "context_id": "…",
 "parts": [{"kind": "text", "size": 31}]}
```

`status` is `success`, `input_required` (with the remote's `task_id` and
`context_id`, and a message that a person must answer; answering is P0c) or
`error` with an `error_type`: `card_refused`, `remote_error`, `reply_refused`,
`remote_task_<state>` (the task ended `failed`, `canceled`, `rejected`…),
`timeout`, `invalid_request` or `misconfigured`. Only text parts come back;
other parts are listed in `dropped_parts`. A remote call counts as a
delegation for AEP-027 and as a remote call for the run's ledger.

## Runs

Each run gets its own `AgentGateway` (`app_name="orrery-studio"`, an in-memory
session store, no memory service, requester-verified confirmation) and a fresh
plugin list from `default_plugins(autonomy_level="L2")`. Its session id is
`s-<run id>`, which `is_machine_session()` keeps out of long-term memory.
MCP toolsets are built for the run and closed when it ends, whatever the
outcome.

**The composition ledger** counts the whole run: tokens (from each model
response's usage), tool calls, delegations, remote calls, and the deadline.
The first budget to run out emits `budget.hit` and ends the run as
`budget_exhausted`; a tool call past a budget is refused before it runs.

**Outcomes:** `succeeded`, `failed` (an error, a model call that failed, or a
labelled node that answered without one of its labels: the run stops at that
node), `cancelled`, `budget_exhausted`.

**Cancel** cancels the run's task: model and HTTP calls in flight stop and
toolsets close. A synchronous tool already running in a worker thread cannot
be interrupted and finishes in the background, harmlessly at L2. Remote tasks
are not sent `tasks/cancel` in P0a.

**Where runs live.** The replica that starts a run runs it, and writes it and
its events to the store as it goes (every quarter second), each batch in one
transaction with the run's row. A failed write is sent again with backoff (up
to 5 s apart; a stored event is not written twice), and the last one is tried
8 times before the run is left to read as lost. A run is visible only
to the person who started it: to anyone else it is a 404, on every replica.

**Several replicas.** With PostgreSQL, any replica serves `GET /studio/runs`,
`GET /studio/runs/{id}` and `/events`: a finished run is replayed from the
store, and one still running elsewhere is followed there until it ends, by
polling the store every second, slowing to every 5 seconds while nothing
happens. A person may follow at most 4 such runs at once; a fifth stream is a
429.
**Cancel is local**: on another replica, `POST /studio/runs/{id}/cancel` for a
run still going is a 409 `"this run is on another replica; only that replica
can cancel it"`. Route a console's requests to one replica (sticky sessions)
to cancel from anywhere. A run whose replica died stays `running` in the store
until its deadline plus two minutes, then reads as `failed`
(`"the replica running it stopped before it ended"`). Without PostgreSQL each replica knows
only its own runs.

## API

All routes need a human admin's bearer token.

| Route | Body / query | Answer |
|---|---|---|
| `POST /studio/runs` | `{"spec": {…}, "input": "…"}` or `{"composition_id": "…", "version"?: 2, "input": "…"}` (input 1–8192 chars; exactly one of `spec` and `composition_id`) | 202 `{"run_id": "…"}`; 422 `{"detail": {"errors": [{loc, msg, code}]}}`; 404 for an unknown composition or version; 429 when too many runs are in flight or the rate limit is hit |
| `GET /studio/runs` | `?composition_id=`, `?limit=` (default 50, at most 200) | `{"runs": [summary…]}`: the caller's own runs, newest first |
| `GET /studio/runs/{id}` | | The run's summary (below) |
| `GET /studio/runs/{id}/events` | `Last-Event-ID` header to resume | `text/event-stream`: every event so far, then each new one, until `run.end` |
| `POST /studio/runs/{id}/cancel` | | The summary; `status` becomes `cancelled` once the task stops; 409 when another replica runs it |
| `POST /studio/validate` | `{"spec": {…}}` | 200 `{"ok": true, "spec_digest": "…", "warnings": [{loc, msg, code}]}`, or the same 422 as a run. Never calls a model or a remote |
| `GET /studio/compositions` | | `{"compositions": [{id, name, description, pattern, version, created_by, created_at, updated_by, updated_at}…]}` |
| `POST /studio/compositions` | `{"spec": {…}}` | 201, the composition with `spec` and `versions`; 422 as a run |
| `GET /studio/compositions/{id}` | `?version=` | The composition (latest, or that version) with `spec` and `versions` (every version number) |
| `PUT /studio/compositions/{id}` | `{"spec": {…}, "version": <the version edited>}` | 200, the new version; 409 `{"detail": {"message", "current_version"}}` when someone saved since |
| `DELETE /studio/compositions/{id}` | `?version=<latest>` | 204, every version gone; 409 as `PUT` |
| `GET /studio/schema` | | `{"spec_version", "spec": <JSON Schema>, "events": <JSON Schema>, "capabilities", "budget_ceilings"}` (below) |
| `GET /studio/registry` | `?resolve=true` fetches every card and lists every MCP server's tools first, concurrently, at most 5 s each (a slow one shows its error) | MCP servers and their tools, A2A entries with card status, usable catalogue tools. Never secrets or env var names |

Compositions are shared by the deployment's admins (anyone may edit, and
`updated_by` says who did); runs are their owner's alone. Every save makes a new
version and keeps the latest 50, so a run can name the one it ran. Saving
validates the spec as a run would. Saves and deletes are limited to 30 a
minute per person, and `?resolve=true` on the registry to 6 a minute (429 with
`Retry-After`).

`capabilities` says what this server runs, so a console offers only that:

```json
{"node_kinds": ["llm_agent", "router", "parallel", "join", "supervisor"],
 "unsupported_node_kinds": {"remote_a2a": "P1; …", "human_approval": "P0c"},
 "patterns": ["pipeline", "fan_out_fan_in", "router", "supervisor", "critic_loop",
              "debate", "map_reduce", "plan_and_execute"],
 "unsupported_patterns": {"handoff": "…"},
 "routing": true, "loops": true, "parallel": true, "supervisor": true,
 "edge_screen": true, "per_node_models": true,
 "models": ["anthropic/claude-sonnet-4-5", "gemini-2.5-flash"],
 "human_approval": false, "stored_compositions": true,
 "limits": {"max_nodes": 30, "max_parallel_branches": 6, "max_supervisor_depth": 3,
            "max_loop_iterations": 5, "max_input_chars": 8192}}
```

`budget_ceilings` is `{max_tokens, max_tool_calls, max_delegations,
max_remote_calls, timeout_s}`, the `ORRERY_STUDIO_MAX_*` values: a spec's
budgets may not exceed them, and a budget it leaves out is set to them.

In the registry, each MCP tool has `description` and `input_schema` as the
server stated them when the tool was pinned (the first run, or
`?resolve=true`, that listed it), and `null` before; each server has an `error`
when its tools could not be listed. There is no "try it" call yet.

`GET /me` has `studio_available`: true only when the Studio is mounted and
the caller is a human admin, so a console can show its Studio entry only then.

The run summary:

```json
{"run_id": "9d13…", "status": "succeeded", "spec_name": "release-notes",
 "spec_digest": "5815…", "pattern": "critic_loop", "composition_id": "c-…",
 "composition_version": 2, "created_at": 1792871739.6, "ended_at": 1792871752.1,
 "error": null, "totals": {…}, "event_count": 11, "dropped_events": 0,
 "cancellable": false}
```

The SSE stream sends one frame per event, `id` being its `seq`, and a
`: keepalive` comment every 15 seconds while nothing happens:

```text
id: 6
event: a2a.message
data: {"seq":6,"run_id":"9d13…","ts":1792871745.2,"type":"a2a.message","node_id":"writer",…}
```

Try it with a token for a human client:

```bash
curl -s -X POST http://127.0.0.1:8000/studio/runs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"spec\": $(cat deploy/studio-demo/specs/critic-loop.json), \"input\": \"Kafka consumer lag\"}"
curl -sN http://127.0.0.1:8000/studio/runs/$RUN_ID/events -H "Authorization: Bearer $TOKEN"
```

## Trace events

Every event has `type`, `seq` (1, 2, … within the run), `run_id` and `ts`
(Unix seconds). `GET /studio/schema` publishes the exact schema
(`orrery_core/studio/events.py`).

| `type` | Fields |
|---|---|
| `run.start` | `spec_name`, `spec_digest` (JCS SHA-256), `pattern`, `nodes` (ids in graph order), `budgets` (resolved), `composition_id`, `composition_version`, `content`? |
| `node.enter` | `node_id`, `kind`, `model`, `parent` (the supervisor, for a child) |
| `node.exit` | `node_id`, `kind`, `model`, `parent`, `tokens` `{input, output, total}`, `elapsed_ms`, `outcome` (`ok` · `failed` · `cancelled` · `budget_exhausted`), `content`? |
| `route.decision` | `node_id` (where the choice was made: the labelled node, or its `router`), `source` (the labelled node), `label`, `alternatives` (its labels), `to` (absent when no edge takes the label and the branch ends) |
| `loop.iteration` | `edge` (`"critic->writer"`), `from_node`, `to_node`, `iteration` (1…cap), `cap`, `stopped` (true at the cap: the edge is not taken) |
| `mcp.call` | `node_id`, `entry`, `tool`, `classification`, `outcome` (`success` · `failure` · `refused`), `status`, `elapsed_ms` |
| `a2a.message` | `node_id`, `remote`, `skill`, `direction` (`outbound` · `inbound`), `method`, `task_id`, `context_id`, `task_state`, `states` (each state seen), `parts` `[{kind, size}]`, `elapsed_ms`, `status`, `card_digest`, `content`? |
| `budget.hit` | `budget` (`max_tokens` · `max_tool_calls` · `max_delegations` · `max_remote_calls` · `timeout_s`), `limit`, `used` |
| `run.end` | `outcome` (`succeeded` · `failed` · `cancelled` · `budget_exhausted`), `error`, `totals` `{tokens, tokens_by_model, tool_calls, delegations, remote_calls, wall_ms}`, `dropped_events`, `content`? |

**No content by default.** Events carry names, ids, states, sizes, counts and
timings, never arguments, results or message text. With
`ORRERY_STUDIO_CAPTURE_CONTENT=true`, `content` (marked ? above) carries text
after PII redaction (credentials and email addresses), cut to 2 KB: the run's
input, each node's answer, each A2A request and reply, and the run's output.
A remote reply is captured as the model saw it, after the safety screen (an
injection attempt shows as the screen's marker), so its `a2a.message` is sent
when the tool call returns. With `ORRERY_REDACT_IPS=true`, IP addresses are
redacted too. A run holds at most `ORRERY_STUDIO_MAX_CONTENT_BYTES_PER_RUN` of
content; later events carry none.

Router, loop, `parallel` and `join` nodes also get `node.enter` and
`node.exit` (no model, no tokens), so a trace shows every step. Each entry of
a node is its own enter/exit pair with its own tokens: two calls of one
supervisor child at once are two pairs, and a node a loop runs again counts
from zero each time. Parallel
branches' events interleave: each branch's `node.enter` comes before any of
them exits.

**At most 2000 events per run.** Past that, events are dropped (the last few
slots are kept for `budget.hit` and `run.end`), and `run.end` says how many.

## The demo

The `studio-demo` compose profile runs the release-notes critic loop
(`deploy/studio-demo/specs/critic-loop.json`) with a remote agent built in
another framework:

- `demo-writer`: a plain `a2a-sdk` agent, no LLM, deterministic
  (`deploy/studio-demo/writer/writer_agent.py`), served over **https** on the
  compose network only, as `demo-writer.studio.internal`;
- `studio-demo-certs`: a one-shot job generating a throwaway internal CA and
  the writer's certificate (`deploy/studio-demo/make_certs.py`). It keeps them
  across restarts, and makes new ones when one is missing, expires within 7
  days or no longer matches the CA. The CA goes to its own volume: Orrery
  mounts only that one (`ORRERY_A2A_CA_FILE=/ca/ca.pem`) and never sees the
  writer's key. After a renewal (or a changed host name, which also renews),
  restart both `demo-writer` and `orrery-studio`: each reads its files once,
  at startup. The registry never names files;
- `orrery-studio`: Orrery with the Studio on, the demo registry
  (`deploy/studio-demo/registry.yaml`: Tirraflow's public MCP server and the
  writer), and the writer's host in `ORRERY_A2A_PRIVATE_ALLOW`.

It binds `127.0.0.1:8000`, like the `demo` profile: run one or the other.

```bash
make dev-token >/dev/null                     # creates ~/.cache/orrery/jwt-secret once
export STUDIO_DEMO_JWT_SECRET=$(cat ~/.cache/orrery/jwt-secret)
export GOOGLE_API_KEY=…                       # or MODEL_PROVIDER + its key
docker compose --profile studio-demo up -d
TOKEN=$(make -s dev-token CLIENT=orrery-console)
curl -s "http://127.0.0.1:8000/studio/registry?resolve=true" -H "Authorization: Bearer $TOKEN"
```

then start a run as in [API](#api). `CLIENT=orrery-console` puts the client in
the token's `azp`, which is what makes it a person here: the same token without
it is a service and gets 403.

CI runs a pipeline end to end (`core/tests/test_studio_api.py`) with a stub
model, the writer on a loopback socket over plain http, and a local MCP
server; `test_studio_patterns.py` runs every pattern and template, and
`test_studio_store.py` the store, run history and two replicas sharing one
database.

## The console

Admins see a **Studio** entry (marked *preview*) in the console's sidebar.
The view is a lazy chunk: the chat console does not load React Flow or the
Studio's code until someone opens it. On a deployment without the Studio
(`/studio/schema` answers 404 or 403), the view says so and does nothing else.

**Compose.** The canvas starts empty, with the release-notes demo and the
pattern gallery one click away.

- **Palette.** Drag a node kind onto the canvas, or focus it and press Enter
  to append it after the last node. Kinds the server cannot run yet are shown
  but disabled, labelled *coming in P0b* or *coming in P0c*. *Remote agent* is
  a shorthand: it adds an `llm_agent` with one `a2a:` tool, which is all the
  spec has for it. On screens narrower than 1280px the palette is a rail of
  icons (each keeps its name and blurb for screen readers and as a tooltip).
- **Edges.** Drag from a node's right handle to another node's left handle.
  Delete or Backspace removes the selected nodes or edges.
- **Inspector.** Edit a node's id (edges follow a rename), description,
  instruction and tools. Tools are picked from the registry: MCP tools, remote
  skills and catalogue tools. Anything not classified read-only is shown but
  cannot be added, since runs are L2. The model field is disabled: per-node
  models come in P0b.
- **Patterns.** Pipeline and release-notes run today. Router, fan-out,
  supervisor, critic loop, debate, map-reduce, handoff and plan-execute are
  in the gallery, marked *needs P0b*: you can look at them but not load them.
- **Layout.** The graph flows left to right, or top to bottom when the canvas
  is taller than it is wide (a laptop screen, beside the palette and panels),
  so a pipeline fits at a readable size. Crossing between the two (a window
  resize) lays the graph out again only while nobody has moved a node: once
  you drag one, positions stay put until *Tidy* or the next load. Ranks
  follow the edges, so the layout is the same for the same spec. A loaded
  pattern, import or past run is fitted to the view, never below 75% zoom;
  a graph too big even then starts at its first node, and the rest is a pan
  away. The view stays fitted as the canvas changes size (a banner, a window
  resize) until you pan or zoom. Loading a
  pattern or importing a spec can be undone from the banner it shows.
- **Validation** runs as you edit. It checks the spec against the JSON Schema
  from `GET /studio/schema` and against this release's rules (one pipeline
  path, kinds, tool references against the registry). Problems are marked on
  the node, the field and the edge, and listed under the composition
  inspector. The server still decides: a 422 from `POST /studio/runs` is
  mapped by `loc` onto the same nodes and fields, and stays pinned until that
  part of the spec changes.
- **Keyboard.** Tab moves through nodes and edges on the canvas; Enter selects
  one and opens it in the inspector; Delete removes it; arrow keys move a
  selected node. The panels are WAI-ARIA tabs (arrow keys, Home, End), and
  dialogs trap focus and close on Escape.

**Run.** The *Run* tab takes the input (Ctrl+Enter starts the run) and plays
the event stream:

- the active node is haloed, nodes that have finished show tokens and latency,
  and the edge into the active node is drawn as live;
- budgets fill as events come in, the timeline lists every event, and totals
  (tokens per model, tool calls, remote calls, delegations, wall time) arrive
  with `run.end`;
- a `budget.hit` is shown as its own card, naming the budget and its limit,
  and marks the node that was running;
- if the stream drops, it reconnects (0.5 s, then 1, 2, 4 and 8 s) with
  `Last-Event-ID`, and events already seen are ignored, so nothing is played
  twice;
- when it gives up, or the stream answers 404, it reads the run's summary
  (`GET /studio/runs/{id}`). A run that ended takes its outcome and totals
  from there. A run still going stays locked, with *Cancel run* and
  *Reconnect* (which resumes after the last event seen). A run that can't be
  read at all (gone, or on another replica) ends as *outcome unknown*;
- *Cancel run* calls `POST /studio/runs/{id}/cancel`, also while the run is
  being started (it is sent as soon as the run has an id). The canvas is
  locked while a run is live.

**A2A.** For each remote call in the run: the request (method, parts and
sizes), the task's states (`submitted → working → completed`, or where it
stopped), and the reply. With content capture on, the reply event arrives when
the tool call returns, so other events can come in between; the tab pairs each
reply with the oldest open request from the same node to the same skill. Below
that, each registered remote: its card's skills (those the registry allows, and
those it does not), the pinned digest and how it was pinned, and any card
error. *Fetch cards* calls `GET /studio/registry?resolve=true`.

**MCP.** Each registry server, its transport and network, and its tools with
their classification, and the MCP calls made in the run. There is no try-it
call yet.

**History.** The runs of this browser tab, newest first (the last 20, in
memory only). *Show* puts a past run back on the canvas with the spec it ran
(undoable from the banner) and opens it in the *Run* tab. Tick two runs to
compare them: A is the earlier, and the table gives totals and per-node tokens
and latency, with B − A.

**Export and import.** *Export* copies or downloads the spec as JSON
(`<name>.spec.json`). *Import* takes pasted JSON or a file. It rejects what
is not a spec (including an unknown node kind or the same edge listed twice)
and loads the rest, with problems marked as usual. There is no
ADK Python export yet.

On a narrow screen the canvas is read-only (tap a node to inspect it); the
inspector and the run panel work the same. Everything runs under the
console's CSP (`script-src 'self'; style-src 'self'`): React Flow sets
positions and sizes through the CSSOM, which that policy allows.

**Checking the canvas in a browser.** The unit tests run React Flow in
jsdom, which has no layout, so the fit and the layout direction are covered by
a smoke script instead: Chrome against the built console, with the server's
CSP and canned `/studio/*` answers (no server or model needed). It checks the
direction at 1440×900 and 1920×1080, that a dragged graph survives a resize,
that a graph loaded over another is fitted to its own nodes, that an
oversized import starts at its first node, and that nothing breaks the CSP.

```bash
make web-build
uv run --with playwright==1.55.0 python web/scripts/studio_smoke.py   # --browser chromium without Chrome
```

## Not yet

- `handoff`, `remote_a2a` nodes, a dynamic map width, and an MCP "try it" call.
- Human approval, answering a remote's `input-required`, `acting` remotes and
  registry editing (P0c).
- `tasks/cancel` to a remote when a run is cancelled.
- Auth for registry MCP entries (only `ORRERY_STUDIO_SECRET_*` will be allowed).
- In the console: a validate-only call (validation before a run is the
  client's own), an MCP try-it call and tool schemas, ADK Python export,
  history that outlives the tab, and editing on a phone.
