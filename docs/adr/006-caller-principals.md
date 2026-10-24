# ADR-006: Caller principals — only a verified human approves

**Status:** Accepted
**Date:** 2026-10-23
**Related:** [ADR-001 RBAC](001-rbac.md), [ADR-004 token exchange](004-user-token-exchange.md), [ADR-007 A2A](007-a2a-exposition.md)

## Context

Every guarded action (`@confirm` / `@destructive`) waits for a human decision.
In requester-verified mode the gate accepts a decision only from the verified
subject that raised the action. That rule assumes the subject is a person.

Nothing checks that. A client-credentials token mapped to the operator role can
call `/chat`, trigger `scale_deployment`, then send `approve` itself, and the
gate sees the same subject on both sides. Today no machine callers are
expected, so this is latent. A2A (ADR-007) makes machine callers the normal
case, and a remote agent's message is model output, not a human decision.

ADK's own A2A executor makes the gap worse: it maps `request_confirmation` to
the A2A `input-required` state and resumes the task on the client's reply, so
whoever holds the task answers the confirmation.

## Decision

### 1. Every verified caller is a principal of one kind

`principal_from_claims()` (beside `identity_from_claims()`) classifies each
verified token, server-side and fail-closed:

| Kind | How it is recognised | Role | Can decide on a guarded action |
|---|---|---|---|
| **human** | Its client (`azp`, else `client_id`) is in `ORRERY_HUMAN_CLIENTS` | From the role claim, as today | Yes, for actions it raised |
| **delegated** | A verified RFC 8693 `act` claim: an agent acting for a user | min(user's role, the agent's ceiling) | Never over the agent's channel; the user decides out of band |
| **service** | Anything else on a machine surface, or a client listed in `ORRERY_SERVICE_CLIENTS` | The client's ceiling (`client=role`), default **viewer** | Never |

`ORRERY_HUMAN_CLIENTS` unset keeps today's behaviour on the HTTP console (every
token there is treated as human) and logs a startup warning; setting it is the
production posture. Machine surfaces (A2A) never treat a caller as human,
whatever the token says.

`act` is read only from the verified token. It is never taken from a header,
message metadata or session state.

### 2. Decisions come only from humans

The gateway already rewrites the decision key every turn from the message
text. It now writes `None` for any principal that is not human, whatever the
text. A machine cannot approve by typing.

### 3. Out-of-band approval for actions raised over a machine channel

`POST /confirmations/{action_id}/decision` with `{"decision": "approve" | "deny"}`:

- requires a **human** principal's token on the console audience;
- approve: refused unless the caller is the pending's requester (for a
  delegated action, the user it was delegated by); deny stays open to anyone,
  as on every other transport;
- approve marks the pending approved in the shared confirmation store (the
  two-phase API the Slack and Chat cards already use) and is audited
  (`confirmation_approved`, `channel=out_of_band`).

The strict gate gains one path: with no in-turn decision, it accepts
`consume_approved(requester, tool, args_hash)` within the approval validity
window. Only the endpoint above can mark a pending approved for a strict-mode
requester, so this adds no way to approve, only a second place for the same
human to do it.

### 4. Pure service principals cannot hold guarded actions

A service principal has no human behind it, so no one can approve its
actions. Its autonomy is pinned to **L2** (read-only) by the server, on top of
its role ceiling. Guarded tools are refused (`BLOCKED`), never queued for an
approval that cannot come.

### 5. One audience per surface

Each surface verifies tokens issued for it: the console keeps `JWT_AUDIENCE`;
A2A requires `ORRERY_A2A_JWT_AUDIENCE` and refuses to start without it. A token
for one surface is rejected by the other.

### 5b. Conversations are per principal

Sessions are stored under the user's subject, which a delegated agent shares.
On every surface a non-human principal sees and continues only the sessions
minted for it (`a2a-*` on A2A, `m-<principal digest>-*` on the console), and
its turns neither feed nor read the user's long-term memory. A person sees
everything stored under their subject.

### 6. Attribution

Audit entries and metrics gain `principal_kind`, `client_id`, `on_behalf_of`
and `channel`, so "who did this" distinguishes a person from an agent acting
for them.

## Consequences

- A deployment that lets machines call `/chat` today must set
  `ORRERY_HUMAN_CLIENTS` to keep them from approving. The warning says so.
- Delegated agents need an IdP that issues RFC 8693 tokens with `act`
  (Keycloak does). Without it, an agent is a service principal: read-only.
- The approval endpoint is a second approval surface; it is pinned by the same
  requester rule and tests as the in-turn path.

## Alternatives considered

- **Refuse every guarded tool to every machine caller.** Simpler, but an agent
  acting for an operator could never propose a fix. Pinning *service*
  principals to L2 keeps that safety where no human exists, without closing the
  delegated case.
- **Let a machine approve if its role allows the action.** This is what the
  code does by accident today, and it is the confused deputy ADR-004 exists to
  avoid.
