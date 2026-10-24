# ADR-008: Consuming Remote A2A Agents

**Status:** Proposed
**Date:** 2026-10-24
**Related:** [ADR-007 A2A exposition](007-a2a-exposition.md), [ADR-006 caller principals](006-caller-principals.md), [AEP-027 run budgets](../enhancements/aep-027-run-budgets.md), [AEP-031 Orrery Studio](../enhancements/aep-031-orrery-studio.md)

## Context

[ADR-007](007-a2a-exposition.md) made Orrery callable over A2A and left the other
direction for later: "consuming remote agents, wrapped like
`GuardedMcpToolset`". AEP-031's Studio needs it now: another team's agent, in
another framework, reached by its agent card.

ADK 2.12 ships `RemoteA2aAgent` (`google.adk.a2a.agent`, `@a2a_experimental`).
It already does more than its reputation:

- `_resolve_agent_card` requires https for a card URL (plain http only on
  loopback), and `_validate_card_rpc_targets` requires every RPC interface to
  be https and on the card's origin;
- httpx does not follow redirects, and nothing in a2a-sdk or ADK enables it.

What it does **not** do, and what Orrery has to own:

- **No address check.** A card URL or RPC target on a private, link-local or
  metadata address is accepted. There is no SSRF guard.
- **No size bound** on cards or replies.
- **A card passed as an object skips the origin check** (`_validate_card_rpc_targets`
  returns early). Pinning a card means passing the object, so Orrery must run
  that check itself.
- **As an agent, it sends the whole session history.** Its default context
  builder renders every earlier event, including other agents' tool results as
  `Tool X returned: {json}`, into the outgoing message. Used as a sub-agent or
  a graph node, a remote agent receives Kafka, Kubernetes or MCP output it was
  never meant to see.
- **As an agent, nothing guards it.** Its reply is an agent event, not a tool
  result. `SafetyScreenPlugin` and `PIIRedactionPlugin` hook `before_run` and
  `after_tool`, `DelegationGuardPlugin` counts `AgentTool` calls, and autonomy,
  RBAC and the confirmation gate are all `before_tool`. None of them would see
  a remote agent used as a node.

## Decision

**A remote agent is always a tool, never a bare agent or graph node.**
`GuardedRemoteA2aTool` (`orrery_core/a2a/remote.py`) is a `BaseTool` that sends
one request to one registered remote and returns its reply. It holds a
`RemoteA2aAgent` internally for the protocol and event conversion (so A2A
version changes follow ADK), runs it in a fresh in-memory session so that
**only the request** leaves, and is labelled with `set_guard()` like every
other tool. That inner run has **no plugins** (a `Runner` built with `plugins=[]`): the
outer tool call is already guarded, and an inner `SafetyScreenPlugin.before_run`
would block legitimate requests that quote suspicious text. The tool carries a
`delegates_remotely` marker that `DelegationGuardPlugin` and `ProgressPlugin`
check alongside `isinstance(tool, AgentTool)`, so a remote call counts as a
delegation. Its function name is derived safely (`a2a_<entry>_<skill>`): model
providers allow no `.` or `:` in tool names. Every existing tool-level control therefore applies unchanged:
autonomy, RBAC, confirmation, AEP-027 counting, the safety screen, PII
redaction, the output cap and the audit log. A wiring test fails on any other
use of `RemoteA2aAgent` in the codebase.

### Where remote agents come from

Only from a registry: the `a2a:` section of the YAML file named by
`ORRERY_STUDIO_REGISTRY` (deployment config, like `ORRERY_MCP_CONFIG`), never
from a request or the console in v1. An entry has `card_url`, the `skills` it may be used for,
`effect: read_only | acting`, optional auth and `private_network`.

- **Pinning.** The card is fetched once and pinned by the SHA-256 of the
  canonical JSON (RFC 8785) of the fields that matter for trust: RPC
  interfaces and URLs, security schemes, skills, and protocol version. A
  remote that changes its description keeps working; one that changes where it
  sends us, or what it claims to do, is disabled until an admin re-pins it.
  (Orrery's own extended card changes with every catalog edit, so pinning raw
  bytes would make it unpinnable.)
- **Origin.** Orrery runs the RPC-target check itself on the pinned object:
  https, and an origin in the entry's allowed set (the card's origin by
  default, plus any service origin the entry names).

### The HTTP client

The MCP guard's `GuardedTransport` cannot be reused as it is: with mcp 2.x it
subclasses `httpx2.AsyncHTTPTransport`, while a2a-sdk and `RemoteA2aAgent` use
`httpx`. The address and origin checks move into a library-neutral function in
`tools/mcp_network.py`, and a second transport for `httpx` is built on it,
taking a **set** of allowed origins. Both transports keep the same rules:

- public addresses only, unless `private_network` is set and the host matches
  `ORRERY_A2A_PRIVATE_ALLOW` (hosts or CIDRs fixed in deployment config);
- metadata and link-local addresses always blocked; proxy variables ignored;
- reads capped while streaming (card and reply bytes, part count), before any
  plugin sees the data;
- connect, read and total timeouts from the entry (defaults 5 s / 60 s / 120 s);
- TLS verified against the system store, plus one deployment-level CA bundle
  if `ORRERY_A2A_CA_FILE` is set (for an internal CA such as the demo fleet's).
  Registry entries never name files.

Plain http is allowed only loopback to loopback, as ADK and the MCP guard
already allow; tests use that.

The client is supplied through `a2a_client_factory` (the `httpx_client`
argument is deprecated in 2.12). ADK's `auth_scheme` is not used: it can raise
auth-required events back to the caller.

### What goes out

- Text only (as ADR-007 accepts only text in), capped at 8192 characters.
- `x-orrery-hop` (`HOP_HEADER`) is always set. Orrery's inbound adapter
  refuses any request that carries it, so this stops **direct** Orrery-to-Orrery
  calls. It does not stop A → B → Orrery loops through agents that do not copy
  the header (LangGraph, plain a2a-sdk agents). Those are bounded by the
  delegation budget and the run deadline instead.
- Credentials: none by default. A bearer token only from an env var whose name
  starts with `ORRERY_STUDIO_SECRET_` and that the entry binds to its own origin.
  The calling user's token is **never** forwarded. Delegated identity needs
  RFC 8693 token exchange ([ADR-004](004-user-token-exchange.md)) with the
  remote as audience.

### What comes back

- The tool returns a dict, `{"status", "reply", "task_state", "parts"}`, never
  a bare string. `SafetyScreenPlugin` returns a replacement when it neutralizes
  a bare string, which ends the after-tool chain, so PII redaction, the audit
  outcome and the output cap would never run. With a dict, every plugin edits
  it in place.
- A remote `input-required` is returned as `task_state: input-required` with
  the question and the remote's `task_id` and `context_id`. It goes to a human
  through the same out-of-band path as an approval (AEP-031), never answered by
  a model on its own; the answer is sent on that same task (P0c).
- Each exchange emits an `a2a.message` trace event (AEP-031).

### Effect and approval

- `effect: read_only`: an ordinary read; allowed at L2.
- `effect: acting`: the tool is `@destructive`. It needs L4 and the
  requester-verified confirmation, as ADR-007 planned. Remote agents that act
  are therefore unavailable at the default L2 ceiling, on purpose.
- Every call counts toward AEP-027's per-run tool and delegation budgets.

## Implementation notes (P0a)

Where the shipped code (AEP-031 P0a) differs from, or makes precise, the
decision above, and why:

- **No plugins on the inner run.** ADK 2.12's `Runner` has no
  `include_plugins` argument; the inner run is a `Runner` built with
  `plugins=[]` and a fresh `InMemorySessionService`, closed after each call.
- **Pinning is optional, and re-checked.** Without `card_sha256` the first
  fetched card is pinned (trust on first use) and the digest is logged, as the
  MCP adapter does for tool descriptions; `GET /studio/registry?resolve=true`
  shows it for review. The card is fetched again every `card_refresh_s`
  (default 300 s) and refused if its digest changed. Cards are fetched on first
  use, not at startup, so a remote that is down does not stop Orrery starting.
  The trust fields are `supportedInterfaces` (URL, binding, protocol version),
  `securitySchemes`, `securityRequirements` and `skills` without `examples`.
- **The tool's description is Orrery's own.** It names the entry and skill,
  adds the registry entry's `description` (operator config), and says the reply
  is data. The card's description and skill text never reach the model's tool
  declarations.
- **The private allowlist is checked twice.** At startup, a `private_network`
  entry needs a non-empty `ORRERY_A2A_PRIVATE_ALLOW`, and a literal-address
  host must be in it. Hostnames are checked on every connect against what they
  resolve to then, since a host may be allowed through a listed network. A
  loopback host may reach loopback without any list, as the MCP guard allows;
  a listed host that resolves to loopback is refused (the list is for the
  private network, not the replica itself).
- **Missing secrets fail startup**, not the first call.
- **Email addresses in replies are redacted.** `PIIRedactionPlugin` redacts
  credentials, not contact details. A remote reply is someone else's text, so
  the tool carries a `redact_contact_details` marker and the plugin redacts
  email addresses in its results too. Other tools are unchanged.
- **The result** also carries `task_id` and `context_id` (always),
  `error_type` on errors, `truncated` when the reply was cut to
  `max_reply_chars`, and `dropped_parts` (the kinds of non-text parts, which
  never reach the model). The part count is checked after the exchange; bytes
  are capped while they stream.
- **Status values** are `success`, `input_required` and `error`, not the task
  state; `task_state` carries that (`completed`, `input-required`, …).
- **No `tasks/cancel`** is sent when the calling run is cancelled; the
  exchange's HTTP call is cancelled and the remote task may finish.
- **Only JSON-RPC** interfaces are used. A gRPC channel would open its own
  connection outside the guarded httpx client. Only the JSON-RPC interfaces
  must pass the origin and https checks; a card that also lists gRPC or
  HTTP+JSON is accepted, and those interfaces stay in the digest.
- **No compression.** Every request sends `Accept-Encoding: identity`, and a
  compressed response is refused before decoding: httpx decodes after the
  transport, so a byte cap on the wire would not bound what is read.
- **JCS numbers** follow RFC 8785 (ECMAScript `Number::toString`): integers
  up to 2^53 as written, everything else as the shortest round-trip double.
- **One SSL context per network**, built once and shared by every client
  (card fetches, RPCs and Studio MCP entries).
- **`?resolve=true`** fetches the cards concurrently, at most 5 s each; a slow
  card is reported in its status rather than holding the response.

## Amendment: an explicit egress proxy

A deployment whose only way out is a forward proxy (an internal-only network
behind squid, say) could not reach a public remote at all, since the guarded
transports ignore proxy variables. They still do: `HTTPS_PROXY` is never read.
Instead the operator names the proxy, `ORRERY_STUDIO_EGRESS_PROXY`
(`http://host:port`), and both transports (A2A's and the Studio's MCP
entries') then:

- send **public** destinations through it with `CONNECT`. TLS runs inside the
  tunnel, end to end, verified against the same roots (and
  `ORRERY_A2A_CA_FILE`) as a direct connection; the proxy sees only the host
  and port;
- check the **origin** on the hostname, as before;
- cannot check the **resolved address**: the proxy resolves the name.
  **The proxy must enforce its own destination allowlist** (squid's
  `http_access` on `CONNECT` targets, for instance); Orrery's origin check
  limits what it asks for, the proxy what it reaches;
- send **loopback, private literal addresses and hosts in
  `ORRERY_A2A_PRIVATE_ALLOW`** direct, never through the proxy, with the full
  address check;
- keep the byte caps, identity encoding and timeouts, which are applied around
  whichever connection is used.

A bad value fails startup. Unset (the default), nothing changes.

## Consequences

- One reviewed path for every outbound A2A call, with the same controls as
  every other tool.
- A remote agent sees only what the calling model chose to send it, never the
  session.
- A remote agent is not a peer in a graph. A Studio "remote agent" node is an
  `llm_agent` node holding this tool (AEP-031).
- Depending on an experimental ADK class: ADK stays pinned, and a contract test
  suite against the demo fleet's agents (ADK, LangGraph, plain a2a-sdk) catches
  upstream changes.
- Re-pinning is an admin step whenever a remote changes its interfaces or
  skills. That is the point, and a cost.

## Alternatives considered

- **`RemoteA2aAgent` as a sub-agent or graph node.** It is what ADK offers, and
  it would bypass every tool-level guard and send the whole session out.
  Rejected.
- **Our own client on a2a-sdk, no ADK class.** Full control, but we would
  re-implement event conversion that ADK maintains. Revisit if `RemoteA2aAgent`
  stays experimental or blocks the guards.
- **Remote agents behind an MCP bridge.** Loses A2A task semantics (states,
  `input-required`, artifacts), which is what the Studio exists to show.
