# ADR-004: Per-user tokens for systems that can't impersonate

**Status:** Accepted (MCP implemented; Kafka and HTTP integrations pending) · **Date:** 2026-10-15 · **Related:** [Acting as the user](../impersonation.md)

## Context

[Impersonation](../impersonation.md) makes Kubernetes and Elasticsearch calls
act as the requesting user. Other systems have no such header: Kafka
authenticates the *connection* (SASL/OAUTHBEARER), Grafana and many MCP servers
take a bearer token, and a token minted for Orrery is the wrong audience for
any of them.

Forwarding the user's own console token is not an option. It is scoped to
Orrery's audience, and passing it on would let any downstream system replay it
against Orrery.

## Decision

Use **OAuth 2.0 Token Exchange (RFC 8693)** at the identity provider (Keycloak
supports it), on demand, per user and per target:

1. The front door keeps the verified *subject token* for the turn, in the same
   turn-scoped context as `CallerIdentity`. It is never written to session
   state, and never visible to tools or the model.
2. A credential broker exchanges it for a short-lived token with the target's
   **audience** and minimum **scope**. Results are cached per `(user, target)`
   until shortly before expiry, in memory only.
3. Clients ask the broker at call time:
   - **Kafka**: an `oauth_cb` callback that returns the user's token, with one
     producer/admin client **per user** (SASL authenticates the connection, not
     the request), LRU-bounded;
   - **MCP servers**: the existing `header_provider` hook on `McpToolset`;
   - **HTTP integrations**: an `Authorization` header in the shared session
     helpers, the same choke points impersonation uses.
4. Fail-closed exactly as impersonation: no subject token, failed exchange, or
   a target without an exchange policy means the call is refused.

## Consequences

- An IdP round-trip on the first call per user and target. The cache makes the
  rest free.
- Kafka moves from one shared client to a per-user pool. This is the largest
  change, and it needs load testing (AEP-016).
- The subject token has to reach the server. The console already sends it on
  every request. Slack and Google Chat don't hold user OAuth tokens, so those
  transports keep impersonation (where available) and the service account
  elsewhere. Linking chat users to IdP sessions is a separate piece of work.
- Refresh tokens are never stored. A user whose token has expired re-authenticates
  in the console, the same as for any other call.

## Implementation notes (MCP)

- The verified bearer is kept on `AuthContext.token` and
  `CallerIdentity.subject_token`, both out of `repr` and equality, and never in
  `AuthContext.as_state()`. It reaches the broker through the turn's existing
  caller ContextVar, so only surfaces that build a `CallerIdentity` (the
  console and A2A, not a service principal) have one.
- `security/token_exchange.py`: `TokenBroker` caches by the subject token's
  digest, audience and scope rather than by user, so nothing outlives the token
  it came from. An exchange runs in its own task, so a caller cancelled
  mid-exchange doesn't cancel it for others waiting on the same key.
- MCP servers opt in with `auth: {type: token_exchange, audience, scope}`. ADK
  pools MCP sessions and caches tool lists by the merged headers, so each
  user's token gets its own session. The credential is resolved in the call
  wrapper before ADK runs, because ADK turns an exception from its header
  provider into a bare `{"error": ...}`. A refusal is `access_denied`, so the
  breaker ignores it; an unreachable IdP is `error`, which it counts, and so is
  a misconfigured client (`401`, `invalid_client`, `unauthorized_client`),
  which is also logged at ERROR and audited, since it fails every user at once.
  A caller without a token is offered no tools from the server.
- Failures are cached per key (a refusal for 30 s, an outage with exponential
  backoff), and exchanges run on a dedicated four-thread pool, so a refused
  user or an IdP brownout costs neither an IdP call per model step nor the
  default executor the blocking tools share.
- Only `token_type: Bearer` with `issued_token_type` of an access token is
  sent: RFC 8693's `N_A` marks a token that is not an access token.

## Alternatives considered

- **Forward the user's token as is.** Rejected: wrong audience, and it can be replayed.
- **Store per-user long-lived credentials.** Rejected: that's a credential vault
  Orrery would have to secure, rotate and audit.
- **ADK's tool authentication (`AuthConfig`).** It suits interactive OAuth
  consent per tool, and stores tokens in session state. Rejected for platform
  integrations for that reason. It may still fit user-consented third-party
  tools later.
