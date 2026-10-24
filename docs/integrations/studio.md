# Orrery Studio (preview)

The Studio runs a **composition**: local agents, remote [A2A](a2a.md) agents
and [MCP servers](mcp.md), wired into a pattern, run apart from the live
coordinator and traced step by step. This page covers P0a of
[AEP-031](../enhancements/aep-031-orrery-studio.md): the HTTP API, the
endpoint registry, a sequential pipeline of `llm_agent` nodes, and the trace
events. There is no canvas yet; that is P1.

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
| `ORRERY_STUDIO_RUN_RETENTION_SECONDS` | `3600` | A finished run is forgotten after this long. |
| `ORRERY_STUDIO_MAX_RUNNING_PER_USER` | `2` | Runs one person may have in flight; more is a 429. |
| `ORRERY_STUDIO_MAX_RUNNING` | `8` | Runs in flight on the replica. |
| `ORRERY_STUDIO_RATE_LIMIT` | `10/minute` | `POST /studio/runs` per person. |
| `ORRERY_A2A_PRIVATE_ALLOW` | — | Hosts and CIDRs a `private_network` entry may reach (comma-separated). |
| `ORRERY_A2A_CA_FILE` | — | One extra CA bundle trusted for outbound A2A (an internal CA). |
| `ORRERY_STUDIO_SECRET_*` | — | Bearer tokens registry entries refer to by name. |

`ORRERY_DAILY_TOKEN_BUDGET_PER_USER` applies to Studio runs too: every run
charges the same per-process ledger as chat.

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
| `auth.bearer_env` | — | An env var whose name starts with `ORRERY_STUDIO_SECRET_`. Sent only to the entry's origins. Unset at startup: startup fails. |
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
loopback. Redirects are never followed and proxy variables are ignored. A
`private_network` entry whose host is a literal address not in the allowlist,
or any `private_network` entry while the allowlist is empty, fails startup.

## Compositions

A composition is a JSON document (`spec_version: 1`). P0a runs one shape: a
**pipeline** of `llm_agent` nodes, chained by edges. Each node gets the
previous node's answer as its input (the first gets the run's input), and the
last node's answer is the run's output.

```json
{
  "spec_version": 1,
  "name": "release-notes",
  "pattern": "pipeline",
  "budgets": { "max_tokens": 60000, "max_tool_calls": 12, "max_remote_calls": 2, "timeout_s": 180 },
  "nodes": [
    { "id": "research", "kind": "llm_agent",
      "instruction": "Search Tirraflow for the topic and answer with short notes.",
      "tools": ["mcp:tirraflow.search_articles", "mcp:tirraflow.get_article"] },
    { "id": "writer", "kind": "llm_agent",
      "instruction": "Send your input to the demo writer's draft tool; answer with its draft.",
      "tools": ["a2a:demo-writer.draft"] },
    { "id": "critic", "kind": "llm_agent",
      "instruction": "Review the draft and give an improved version." }
  ],
  "edges": [
    { "from": "research", "to": "writer" },
    { "from": "writer", "to": "critic" }
  ]
}
```

`deploy/studio-demo/release-notes.spec.json` is this spec in full.

| Tool reference | What the node gets |
|---|---|
| `mcp:<entry>.<tool>` | The tool from a registry MCP server, as `mcp_<entry>_<tool>`. Read-only tools only. |
| `a2a:<entry>.<skill>` | A `GuardedRemoteA2aTool` named `a2a_<entry>_<skill>` (below). |
| `catalog:<name>` | One of the deployment's own catalogue tools (`GET /studio/registry` lists them). Read-only ones only. |

The format already describes later phases (`router`, `parallel`, `join`,
`supervisor`, `human_approval`, `remote_a2a` nodes; `when`, `max_iterations`
and `screen` on edges; per-node `model` and `output` labels). A spec using any
of them parses, and is refused with a `not_supported_yet` error at that field.
`GET /studio/schema` publishes the JSON Schema.

Validation runs before anything else, and returns every problem at once, each
with the path of the field that caused it:

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

It refuses: duplicate ids; edges to unknown nodes; branches, joins, cycles,
self-loops and unreachable nodes; more than one first node; tool references
that are malformed, unknown, not `read_only` or not registered for the skill;
budgets over the ceilings; unknown keys anywhere; and a `spec_version` newer
than the server reads.

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

**Outcomes:** `succeeded`, `failed` (an error, or a model call that failed: the
pipeline stops at that node), `cancelled`, `budget_exhausted`.

**Cancel** cancels the run's task: model and HTTP calls in flight stop and
toolsets close. A synchronous tool already running in a worker thread cannot
be interrupted and finishes in the background, harmlessly at L2. Remote tasks
are not sent `tasks/cancel` in P0a.

**Where runs live.** In memory, on the replica that started them, for
`ORRERY_STUDIO_RUN_RETENTION_SECONDS`. A run is visible only to the person who
started it; on another replica, or to anyone else, it is a 404.

## API

All routes need a human admin's bearer token.

| Route | Body / query | Answer |
|---|---|---|
| `POST /studio/runs` | `{"spec": {…}, "input": "…"}` (input 1–8192 chars) | 202 `{"run_id": "…"}`; 422 `{"detail": {"errors": [{loc, msg, code}]}}`; 429 when too many runs are in flight or the rate limit is hit |
| `GET /studio/runs/{id}` | | The run's summary (below) |
| `GET /studio/runs/{id}/events` | `Last-Event-ID` header to resume | `text/event-stream`: every event so far, then each new one, until `run.end` |
| `POST /studio/runs/{id}/cancel` | | The summary; `status` becomes `cancelled` once the task stops |
| `GET /studio/schema` | | `{"spec_version": 1, "spec": <JSON Schema>, "events": <JSON Schema>}` |
| `GET /studio/registry` | `?resolve=true` fetches each card first | MCP servers and their tools, A2A entries with card status, usable catalogue tools. Never secrets or env var names |

The run summary:

```json
{"run_id": "9d13…", "status": "succeeded", "spec_name": "release-notes",
 "spec_digest": "5815…", "created_at": 1792871739.6, "ended_at": 1792871752.1,
 "error": null, "totals": {…}, "event_count": 11, "dropped_events": 0}
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
  -d "{\"spec\": $(cat deploy/studio-demo/release-notes.spec.json), \"input\": \"Kafka consumer lag\"}"
curl -sN http://127.0.0.1:8000/studio/runs/$RUN_ID/events -H "Authorization: Bearer $TOKEN"
```

## Trace events

Every event has `type`, `seq` (1, 2, … within the run), `run_id` and `ts`
(Unix seconds). `GET /studio/schema` publishes the exact schema
(`orrery_core/studio/events.py`).

| `type` | Fields |
|---|---|
| `run.start` | `spec_name`, `spec_digest` (JCS SHA-256), `pattern`, `nodes` (ids in run order), `budgets` (resolved), `content`? |
| `node.enter` | `node_id`, `kind`, `model` |
| `node.exit` | `node_id`, `kind`, `model`, `tokens` `{input, output, total}`, `elapsed_ms`, `outcome` (`ok` · `failed` · `cancelled` · `budget_exhausted`), `content`? |
| `mcp.call` | `node_id`, `entry`, `tool`, `classification`, `outcome` (`success` · `failure` · `refused`), `status`, `elapsed_ms` |
| `a2a.message` | `node_id`, `remote`, `skill`, `direction` (`outbound` · `inbound`), `method`, `task_id`, `context_id`, `task_state`, `states` (each state seen), `parts` `[{kind, size}]`, `elapsed_ms`, `status`, `card_digest`, `content`? |
| `budget.hit` | `budget` (`max_tokens` · `max_tool_calls` · `max_delegations` · `max_remote_calls` · `timeout_s`), `limit`, `used` |
| `run.end` | `outcome` (`succeeded` · `failed` · `cancelled` · `budget_exhausted`), `error`, `totals` `{tokens, tokens_by_model, tool_calls, delegations, remote_calls, wall_ms}`, `dropped_events`, `content`? |

**No content by default.** Events carry names, ids, states, sizes, counts and
timings, never arguments, results or message text. With
`ORRERY_STUDIO_CAPTURE_CONTENT=true`, `content` (marked ? above) carries text
after PII redaction (credentials and email addresses), cut to 2 KB: the run's
input, each node's answer, each A2A request and reply, and the run's output.

**At most 2000 events per run.** Past that, events are dropped (the last few
slots are kept for `budget.hit` and `run.end`), and `run.end` says how many.

## The demo

The `studio-demo` compose profile runs the release-notes pipeline with a remote
agent built in another framework:

- `demo-writer`: a plain `a2a-sdk` agent, no LLM, deterministic
  (`deploy/studio-demo/writer/writer_agent.py`), served over **https** on the
  compose network only, as `demo-writer.studio.internal`;
- `studio-demo-certs`: a one-shot job generating a throwaway internal CA and
  the writer's certificate (`deploy/studio-demo/make_certs.py`). Orrery trusts
  it through `ORRERY_A2A_CA_FILE`; the registry never names files;
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

CI runs the same pipeline end to end (`core/tests/test_studio_api.py`) with a
stub model, the writer on a loopback socket over plain http, and a local MCP
server.

## Not yet

- Other patterns, per-node models, stored compositions and replay (P0b).
- Human approval, answering a remote's `input-required`, `acting` remotes and
  registry editing (P0c).
- `tasks/cancel` to a remote when a run is cancelled.
- Auth for registry MCP entries (only `ORRERY_STUDIO_SECRET_*` will be allowed).
- The canvas and inspectors in the web console (P1).
