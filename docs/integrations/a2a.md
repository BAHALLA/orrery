# A2A (agent-to-agent)

Other agents can ask Orrery to investigate, and to propose changes a person
approves, over the [A2A protocol](https://a2a-protocol.org) 1.0 (JSON-RPC).
This is the only way Orrery's agents are exposed to other agents. MCP is how
Orrery *consumes* tools ([MCP servers](mcp.md)); it does not expose agents.

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

## Not yet

- A shared task store, for running the A2A surface on several replicas.
- Card signing (JWS, spec §8.4).
- Linking an inbound `traceparent` to the turn's trace.
- Keeping A2A threads out of the console: today a user's
  `GET /sessions` also lists the threads their agents opened.
- Orrery *calling* remote A2A agents. That will be wrapped like MCP toolsets:
  allowlisted cards, delegation treated as destructive, replies screened.
