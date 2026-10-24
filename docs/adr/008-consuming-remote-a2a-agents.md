# ADR-008: Consuming Remote A2A Agents

**Status:** Proposed
**Date:** 2026-10-24
**Related:** [ADR-007 A2A exposition](007-a2a-exposition.md), [ADR-006 caller principals](006-caller-principals.md), [AEP-031 Orrery Studio](../enhancements/aep-031-orrery-studio.md)

## Context

[ADR-007](007-a2a-exposition.md) made Orrery callable over A2A and left the other
direction for later: "consuming remote agents, wrapped like
`GuardedMcpToolset`". AEP-031's Studio needs it now: a composition node that
is another team's agent, in another framework, reached by its agent card.

ADK 2.12 ships `RemoteA2aAgent` (`google.adk.a2a.agent`). It resolves a card
(object, URL or file), sends A2A messages through an `httpx.AsyncClient`,
converts replies to ADK events, and accepts an `a2a_request_meta_provider`. It
is marked `@a2a_experimental`. Used as it is:

- it fetches whatever card URL it is given, follows the card's own `url`, and
  uses a default HTTP client: no SSRF guard, redirects followed;
- a remote reply flows into the next model call unscreened;
- nothing bounds the reply size or the number of delegations;
- nothing stops two compositions (or Orrery and itself) from calling each other
  forever;
- a remote agent that *acts* (changes a system) looks the same as one that
  answers a question.

That is the same shape of problem as an MCP server, and Orrery already has an
answer for MCP.

## Decision

`GuardedRemoteA2aAgent` in `orrery_core/a2a/remote.py`: a thin subclass/wrapper
of ADK's `RemoteA2aAgent`, never used bare. We keep ADK's protocol and event
conversion (so A2A version changes follow ADK) and own every trust decision.

### Where remote agents come from

Only from a registry entry (AEP-031 `studio_endpoints`, or a future
`ORRERY_A2A_REMOTES` file for the coordinator):

- `card_url`, the `skills` it may be used for, `effect: read_only | acting`,
  optional auth as an env-var reference, and `private_network`.
- The card is fetched once, through the guarded client, and **pinned** by its
  SHA-256. A changed card disables the entry until an admin re-approves it
  (same idea as MCP tool-description fingerprints).
- The card's service `url` must have the card URL's origin, unless the entry
  names it explicitly.

### The HTTP client

The `httpx.AsyncClient` passed to `RemoteA2aAgent` uses the MCP network guard's
transport (`GuardedTransport`, `tools/mcp_network.py`): one origin, no redirects, public addresses only unless
`private_network`, metadata and link-local always blocked, proxy env ignored.
Connect, read and total timeouts come from the entry (defaults 5 s / 60 s / 120 s).

### What goes out

- Text parts only in v1 (as ADR-007 accepts only text in).
- `x-orrery-hop` (`HOP_HEADER`) is always set. Orrery's inbound adapter
  refuses any request that carries it (`a2a_server.py`), so a remote agent
  that calls back into Orrery is stopped at once. AEP-031's "publish a
  composition as A2A" needs compositions to call compositions; that phase
  turns the header into a counter (refused past 3) and amends ADR-007.
- Credentials: none by default. A bearer token from an env var when the entry
  names one. The calling user's token is **never** forwarded; delegated
  identity requires RFC 8693 token exchange ([ADR-004](004-user-token-exchange.md))
  with the remote as audience.

### What comes back

- Replies are capped (bytes and parts) and passed through `SafetyScreenPlugin`
  and PII redaction before any model sees them: a remote agent's text is
  untrusted input, like a tool result.
- Task states are mapped onto Orrery's: `input-required` from the remote is
  surfaced as a pending human question, never answered by a model on its own.
- Each exchange emits an `a2a.message` trace event (AEP-031).

### Effect and approval

- `effect: read_only`: allowed at L2.
- `effect: acting`: every delegation is treated like a `@destructive` tool:
  it needs the requester-verified confirmation gate and L4, as ADR-007
  planned. The remote agent's own approvals are its business; ours happen first.
- Each delegation counts toward AEP-027's per-run delegation budget.

## Consequences

- One reviewed path for every outbound A2A call; no bare `RemoteA2aAgent` in
  the codebase (a wiring test greps for it).
- Depending on an experimental ADK class: pinned ADK version, and a contract
  test suite against the demo fleet's agents (ADK, LangGraph, plain a2a-sdk)
  catches upstream changes.
- Card pinning means a remote upgrade needs an admin click. That is the point,
  and also a cost.
- Streaming of intermediate remote progress is not exposed in v1; the trace
  shows message and task-state transitions.

## Alternatives considered

- **Our own client on a2a-sdk, no ADK class.** Full control, but we would
  re-implement event conversion that ADK maintains. Rejected for now; revisit if
  `RemoteA2aAgent` stays experimental or blocks the guards.
- **Remote agents as MCP tools through a bridge.** Loses A2A task semantics
  (states, `input-required`, artifacts), which is what the Studio exists to show.
