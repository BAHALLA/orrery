# ADR-007: Exposing Orrery over A2A

**Status:** Accepted (supersedes [AEP-005](../enhancements/aep-005-a2a-protocol.md))
**Date:** 2026-10-23
**Related:** [ADR-006 caller principals](006-caller-principals.md)

## Context

Other agents should be able to ask Orrery to investigate, and to propose
changes a human approves, over the [A2A protocol](https://a2a-protocol.org)
(1.0). Orrery's agents are exposed only this way. MCP stays the way Orrery
*consumes* tools; it does not expose agents.

ADK 2.12 ships `to_a2a()` and `A2aAgentExecutor`. Used as they are, they
bypass everything that makes Orrery safe to expose:

- they call `runner.run_async` directly, skipping `AgentGateway.run_in_session`:
  no actor stamp, no per-turn decision rewrite, no `caller_scope`, no role;
- with no authentication middleware every caller runs as
  `A2A_USER_<context_id>` and the session id is the client's `context_id`, so
  anyone who knows a `context_id` shares that session;
- `security_schemes` on the generated card are advertised, never enforced;
- `request_confirmation` maps to `input-required` and the client's reply
  resumes it, so a remote agent approves its own action;
- all of it is marked experimental.

## Decision

A thin, custom A2A adapter on the a2a-sdk (1.x, `orrery-core[a2a]`) whose
**only** path into agents is `AgentGateway.run_in_session`.

### Surface

- `serving/a2a_server.py`, mounted when `ORRERY_A2A_ENABLED=true`. In
  production it runs as its own deployment and host; in development it can
  share the HTTP app.
- JSON-RPC binding, with `SendMessage`, `SendStreamingMessage`, `GetTask`,
  `ListTasks`, `CancelTask` and the agent card. Push notifications are off.
- The coordinator is exposed. Individual specialists can be exposed later as
  separate gateways; the batch triage workflow never is.

### Identity

- Every request carries a bearer token for `ORRERY_A2A_JWT_AUDIENCE`, verified
  by the same code as the console. Anonymous requests are refused before the
  executor runs.
- The caller becomes a principal (ADR-006): **delegated** when the token has a
  verified `act` claim, otherwise **service**. Never human.
- Role and autonomy come from the principal, through the existing `AuthPlugin`
  path. Impersonation uses the delegating user for a delegated principal and
  fails closed for a service principal.

### Messages

- Text parts only. A `DataPart`, a file, or anything that looks like a function
  response is refused, so a client cannot inject an ADK function response.
- Only the final reply text is returned (`extract_reply_text`), never ADK's
  intermediate function-call events.

### Sessions and tasks

- `contextId` is minted by the server and bound to the principal: a client
  cannot pick, guess or reuse another caller's context.
- `taskId` is one turn. Tasks live in a task store scoped by owner (the
  caller's whole principal key), so `GetTask` / `ListTasks` / `CancelTask` reveal nothing
  across callers (spec §13.1). Postgres when `DATABASE_URL` is set, and
  required when replicated.

### Approvals

- When a turn ends with a pending guarded action, the task moves to
  `input-required` with a data part `{action_id, tool, args, level,
  expires_at, approve_url}`. Arguments are redacted like any tool result.
- A reply on the task is **not** a decision (ADR-006 §2). The delegating human
  approves in the console or through the out-of-band endpoint; the client then
  sends any message on the task to resume, and the gate consumes the approval.
- A service principal never reaches this state: it is pinned to L2.

### Agent card

- Built from the live inventory (no drift): one skill per enabled specialist,
  from its `routing_hint`.
- The public card is minimal (name, description, interfaces, security scheme).
  Skills are in the authenticated extended card.
- `securitySchemes` names the real bearer scheme; interfaces are https outside
  loopback.
- Card signing (JWS + JCS, spec §8.4) is a follow-up; ADK has no support for it.

### Limits

- Per-subject rate limit (`ORRERY_A2A_RATE_LIMIT`) and a cap on tasks in
  flight per principal. Run budgets (AEP-027) already bound each run.
- An inbound `traceparent` becomes a span link, not a parent.
- A hop header refuses calls that arrive from Orrery itself, so two agents
  cannot loop at the platform's model cost.

## Implementation notes (first release)

Where the shipped code differs from the decision above, and why:

- **Conversation binding.** The a2a-sdk mints a `contextId` before the executor
  sees the request, so the server cannot tell a minted id from a client's.
  Instead of storing a binding, the ADK session id is derived:
  `a2a-` + SHA-256(principal, contextId), where the principal is kind, client,
  acting agent and subject (two agents delegated by one user share a subject).
  Any id a client sends reaches only its own conversation, the derivation needs no table and works on any replica, and
  the prefix keeps it out of the console's id space.
- **Task store.** In memory, scoped by the same principal key, and bounded
  (`BoundedTaskStore`: a task idle for an hour is dropped, at most 10 000 are
  kept), since the SDK's own store never evicts. Request bodies are capped on
  the stream before the SDK parses them, and a message is capped at the
  console's 8192 characters. The server refuses to start with A2A
  on and `ORRERY_MULTI_REPLICA` set until a shared store exists. The SDK's SQL
  store needs extras that pull MySQL drivers; a Postgres-only store is a
  follow-up.
- **Public card.** The spec requires at least one skill, so the public card
  carries one generic skill; the specialists stay in the extended card.
- **Mounting.** `/a2a` on the HTTP app, behind the same middleware. Running it
  as its own deployment and host needs no code (the same image, its own
  release); the Helm values for that are a follow-up.
- **Not yet:** `traceparent` span links, card signing.
- **A2A threads live under the user's id**, so the person can approve. The
  console keeps machine principals apart the same way A2A does: a non-human
  caller there sees and continues only sessions minted under its own
  `m-` prefix (derived from its principal key), never its user's threads, an
  `a2a-*` one, or another agent's. The person still sees all of them. Long-term
  memory, keyed by the user too, neither stores a machine principal's turns
  nor answers its recalls.

## Consequences

- More code than `to_a2a()`, all of it on paths that already exist (gateway,
  JWT verification, confirmation store).
- Clients resume a task after approval themselves. A server-driven resume can
  come later without changing the protocol surface.
- Consuming remote A2A agents is a separate decision (later), to be wrapped
  like `GuardedMcpToolset`: allowlisted cards, delegation counted as
  destructive, replies screened.
