# A2A (agent-to-agent)

Other agents can ask Orrery to investigate, and to propose changes a person
approves, over the [A2A protocol](https://a2a-protocol.org) 1.0 (JSON-RPC).
This is the only way Orrery's agents are exposed to other agents. MCP is how
Orrery *consumes* tools ([MCP servers](mcp.md)); it does not expose agents.
Orrery can also *call* remote A2A agents, from Studio compositions
([below](#calling-other-agents-outbound)).

The design is [ADR-007](../adr/007-a2a-exposition.md). In short: ADK's
`to_a2a()` runs the agent straight off a `Runner`, with no verified caller, a
session the client names and approvals the client answers itself. Orrery keeps
the protocol and replaces what sits behind it. Every A2A turn goes through the
same gateway as a console turn: RBAC, autonomy, the confirmation gate, run
budgets, redaction and the audit trail all apply.

## Enabling it

```bash
uv sync --extra a2a                          # source installs only: the a2a-sdk (opt-in)
export ORRERY_A2A_ENABLED=true
export ORRERY_A2A_JWT_AUDIENCE=orrery-a2a    # the audience agent tokens are issued for
export ORRERY_A2A_PUBLIC_URL=https://orrery.example.com/a2a
make run-api
```

The published image (`ghcr.io/bahalla/orrery`, from 0.7.0) already includes
the `a2a` extra, so in a container you only set the environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `ORRERY_A2A_ENABLED` | `false` | Serve `POST /a2a` (JSON-RPC) and `GET /a2a/.well-known/agent-card.json`. |
| `ORRERY_A2A_JWT_AUDIENCE` | — (required) | The `aud` an agent's token must carry. Distinct from `JWT_AUDIENCE`, so a console token is never accepted as an agent's. |
| `ORRERY_A2A_PUBLIC_URL` | `http://127.0.0.1:8000/a2a` | The endpoint advertised on the card. Must be `https` outside loopback. |
| `ORRERY_A2A_APPROVAL_BASE_URL` | origin of the public URL | Where a person approves: `{base}/confirmations/{id}/decision`. |
| `ORRERY_A2A_RATE_LIMIT` | `30/minute` | Per calling subject. Shares `ORRERY_RATE_LIMIT_STORAGE_URI`. |
| `ORRERY_A2A_MAX_TASKS_IN_FLIGHT` | `2` | Turns one caller may have running at once; more are rejected. |
| `ORRERY_A2A_MAX_MESSAGE_CHARS` | `8192` | Characters one message may carry (the console's bound); a longer one is rejected. |
| `ORRERY_A2A_MAX_BODY_BYTES` | `131072` | Bytes one request body may carry, checked on the stream before the SDK parses it; more is a `413`. |
| `ORRERY_A2A_MAX_TASKS` | `10000` | Tasks kept in memory; past it the least recently touched are dropped. |
| `ORRERY_A2A_TASK_IDLE_TTL_SECONDS` | `3600` | Integer. A task untouched this long is dropped; its client then gets `TaskNotFound`, as after a restart. |

Startup fails when A2A is on and auth is off, when the audience is missing,
when a URL is plain http outside loopback, or when `ORRERY_MULTI_REPLICA` is
set: A2A tasks are kept in the process for now, so run the A2A surface as a
single replica. Conversations themselves live in the session store as usual.

## Who is calling

Every call carries a bearer token for `ORRERY_A2A_JWT_AUDIENCE`, verified by
the same code as the console's. Anonymous calls get a 401 before the A2A SDK
parses anything. The caller is classified as in
[People, agents and services](../config/security.md#people-agents-and-services-adr-006),
except that on this surface no token is ever a person:

- A token with a verified `act` claim (RFC 8693 token exchange) is a
  **delegated** principal: an agent acting for the user in `sub`. Its role is
  the lower of the user's and the agent client's ceiling in
  `ORRERY_SERVICE_CLIENTS`, and downstream systems that impersonate see the
  user.
- Anything else is a **service** principal: capped at its ceiling (default
  `viewer`), pinned to autonomy L2 (read-only), and impersonating systems
  refuse it.

Neither can approve anything, whatever its message says.

A call carrying `X-Orrery-Hop` came from an Orrery deployment and is refused
(403), so two agents cannot bounce a task between them at your model cost.

## Conversations and tasks

- A *caller* is the whole principal: its kind, client, acting agent and
  subject. Two agents acting for the same user are two callers, and neither
  sees the other's conversations or tasks.
- A `contextId` is one conversation. The session behind it is derived from the
  caller *and* the id, so another caller sending the same `contextId` lands in
  a different, empty conversation, and a delegated agent cannot continue one of
  its user's console conversations.
- A `taskId` is one turn. `GetTask`, `ListTasks` and a follow-up message on a
  task see only the caller's own tasks; anything else is `TaskNotFound`.
- An approval is spent only in the conversation that raised the action.
- Messages must be text parts only. A data or file part rejects the task.
- `CancelTask` is refused: a turn may already have sent a change to a cluster,
  and stopping the wait would not stop it.
- Push notifications are off. Use `SendStreamingMessage` or poll `GetTask`.

## Approvals

When a turn ends on a guarded action, the task moves to `input-required`. Its
status message carries the agent's text and a data part:

```json
{
  "kind": "orrery.pending_action",
  "action_id": "3f6c…",
  "tool": "scale_deployment",
  "level": "confirm",
  "args": {"name": "orders-worker", "replicas": 3},
  "expires_at": 1793270000.0,
  "approve_url": "https://orrery.example.com/confirmations/3f6c…/decision"
}
```

Arguments are redacted like any tool result. The calling agent shows this to
its user, who approves as themselves, in the console or with:

```bash
curl -X POST "$APPROVE_URL" -H "Authorization: Bearer $USER_TOKEN" \
  -H 'Content-Type: application/json' -d '{"decision": "approve"}'
```

The agent then sends any message on the same task (same `taskId` and
`contextId`). Orrery retries the action, the gate finds the approval, and the
task completes. A reply such as "approve" from the agent is not a decision:
only the user who raised the action can approve it, and only out of band.

A service principal never gets here: at L2 a guarded call is refused, not
queued.

## The agent card

`GET /a2a/.well-known/agent-card.json` is public and minimal: name,
description, endpoint, the bearer scheme, and one generic skill. The
authenticated extended card (`GetExtendedAgentCard`) lists one skill per
specialist the coordinator can reach right now, read from the live agent tree,
so it follows `ORRERY_SPECIALISTS` and catalog edits.

## Calling it

With the a2a-sdk client:

```python
import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import SendMessageRequest

headers = {"Authorization": f"Bearer {token}"}  # a token for ORRERY_A2A_JWT_AUDIENCE
async with httpx.AsyncClient(headers=headers, timeout=300) as http:
    client = await create_client(
        "https://orrery.example.com/a2a", client_config=ClientConfig(httpx_client=http)
    )
    request = SendMessageRequest(message=new_text_message("Is the orders topic lagging?"))
    async for event in client.send_message(request):
        print(event)
```

`core/tests/test_a2a_server.py` runs this snippet against the app.

## Calling other agents (outbound)

Orrery calls remote A2A agents from [Studio](studio.md) compositions, and only
through one class, `GuardedRemoteA2aTool` (`orrery_core/a2a/remote.py`, design
in [ADR-008](../adr/008-consuming-remote-a2a-agents.md)). A test fails if
ADK's `RemoteA2aAgent` is used anywhere else.

- **A remote agent is a tool, never an agent node.** The calling model writes
  one `request`; that text is all that leaves. The remote never sees the
  session, other agents or earlier tool results. The tool's reply is a dict
  (`status`, `reply`, `task_state`, `parts`, `task_id`, `context_id`), so the
  safety screen, PII redaction (email addresses included), the output cap,
  RBAC, autonomy, the audit trail and the run budgets all apply. A call counts
  as a delegation.
- **Only registered remotes.** Entries come from the Studio registry
  (`ORRERY_STUDIO_REGISTRY`, deployment config), each limited to the skills it
  lists. Only `effect: read_only` entries are accepted for now; `acting` is
  refused until approvals land (AEP-031 P0c).
- **The card is pinned** by the SHA-256 of the canonical JSON (RFC 8785) of its
  interfaces, security schemes and skills, from `card_sha256` or on first use.
  A changed endpoint, scheme or skill list disables the entry until an admin
  re-pins it. Orrery checks the RPC targets itself: https (http only loopback
  to loopback), JSON-RPC, and on the entry's origins.
- **The network guard** (shared with MCP) checks every request on connect:
  public addresses only, unless the entry sets `private_network` and its host
  or network is in `ORRERY_A2A_PRIVATE_ALLOW`; link-local and metadata
  addresses always refused; no redirects; no proxies; card and reply bytes
  capped while they stream; connect, read and total timeouts.
- **What is sent:** text only (8192 characters at most), `X-Orrery-Hop: 1` on
  every request, and a bearer token only when the entry names one from an
  `ORRERY_STUDIO_SECRET_*` variable, sent only to the entry's origins. The
  caller's own token is never forwarded.
- **TLS** is verified against the system store, plus `ORRERY_A2A_CA_FILE` when
  set (one deployment-level bundle for an internal CA). Registry entries never
  name files.
- A remote `input-required` comes back as `status: input_required` with the
  remote's `task_id` and `context_id`, for a person to answer (P0c); a model is
  told not to answer it.

The hop header means Orrery cannot call another Orrery deployment's `/a2a`
(it refuses the header). Agents that do not forward it can still form a loop;
the run's delegation, remote-call and deadline budgets bound that.

## Not yet

- A shared task store, for running the A2A surface on several replicas.
- Card signing (JWS, spec §8.4).
- Linking an inbound `traceparent` to the turn's trace.
- Keeping A2A threads out of the console: today a user's
  `GET /sessions` also lists the threads their agents opened.
- Calling remote agents from chat specialists (today: Studio compositions only),
  `acting` remotes, and sending `tasks/cancel` when a run is cancelled.
