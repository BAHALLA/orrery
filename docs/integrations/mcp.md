# MCP servers

Connect any [Model Context Protocol](https://modelcontextprotocol.io) server —
GitHub, a ticketing system, an internal API — and Orrery exposes it as one more
specialist the coordinator can route to. Its tools sit behind **the same gates
as Orrery's own**: RBAC, the autonomy levels, the confirmation card and the
audit trail.

That last part is the point. Every gate decides what a tool *does* from its
guard level, which Orrery's tools get from `@confirm` / `@destructive`. A tool
from an MCP server has neither, so plugged in raw it would look like a harmless
read to every gate — a viewer could call `delete_repository` with no approval.
The MCP adapter labels each tool as it is discovered, and **fails closed**.

## Enabling it

```bash
uv sync --extra mcp                       # the MCP SDK (opt-in)
export ORRERY_MCP_CONFIG=/etc/orrery/mcp.yaml
make run-api
```

With `ORRERY_MCP_CONFIG` unset nothing changes and the MCP SDK is never
imported. A file that is set but missing or invalid **stops startup** — running
without the servers, or the guards, you believe are configured is worse than
not running.

## Configuration

```yaml
servers:
  - name: github                       # lowercase; prefixes every tool: github_list_issues
    description: GitHub issues and pull requests for the platform repositories
    transport: streamable_http         # streamable_http | sse | stdio
    url: https://api.githubcopilot.com/mcp/
    headers_from_env:                  # header -> env var holding its full value
      Authorization: GITHUB_MCP_AUTHORIZATION   # e.g. "Bearer ghp_…"
    tools:                             # REQUIRED allowlist; nothing else is exposed
      - list_issues
      - get_issue
      - create_issue
      - close_issue
    read_only: [list_issues, get_issue]
    destructive: [close_issue]
    # create_issue is undeclared → treated as destructive (see below)

  - name: inventory
    description: Warehouse inventory service
    transport: stdio
    command: /usr/local/bin/inventory-mcp
    args: ["--region", "eu-west-1"]
    env_from:                          # child env var -> host env var
      INVENTORY_TOKEN: INVENTORY_MCP_TOKEN
    tools: [list_items, update_item]
    read_only: [list_items]
    trust_annotations: true            # let the server's own hints lower protection
```

| Field | Meaning |
|---|---|
| `name` | `^[a-z][a-z0-9_]{1,30}$`. Becomes the specialist (`github_mcp_agent`) and the tool prefix. |
| `description` | What the coordinator routes on. Write it for the model. |
| `transport` | `streamable_http`, `sse` or `stdio`. |
| `url` | For HTTP transports. **https only** — plain `http` is accepted for loopback, or with `allow_insecure_http: true`. |
| `headers_from_env` | Secrets are referenced by env var, never written in the file. A missing variable fails startup. |
| `command` / `args` / `env_from` | For `stdio`. The child gets only `PATH` plus what `env_from` names — not the host's environment. |
| `tools` | The allowlist. A tool the server offers but this list omits is invisible to the agent. |
| `read_only` / `destructive` | Your reviewed classification. Always wins. |
| `trust_annotations` | Default `false`. See below. |
| `timeout_seconds` | Connection timeout, default 10. |
| `call_timeout_seconds` | Deadline for one tool call, default 60 (max 600). See below. |
| `tool_list_cache_seconds` | How long a tool listing is reused before the server is asked again, default 300; `0` asks every turn. |
| `allow_private_network` | Default `false`. Lets an HTTP server resolve to a private or loopback address (never link-local or metadata). See below. |
| `ca_file` / `client_cert_file` / `client_key_file` | TLS for HTTP transports: a CA bundle, and a client certificate for mTLS (a key needs its certificate). `ca_file` **replaces** the system roots for that server rather than adding to them. Read at startup; a missing file fails it. |
| `auth` | Per-user credentials: `{type: token_exchange, audience, scope}`. See [Per-user credentials](#per-user-credentials). Excludes an `Authorization` entry in `headers_from_env`. |
| `pinned` | `tool: sha256` fingerprints you reviewed (case-insensitive). A pinned tool whose text differs is withheld from the first call. |
| `instruction` | Extra guidance appended to the specialist's prompt. |

## How tools are classified

For each allowlisted tool, first match wins:

1. Listed in `destructive` → **destructive**.
2. Listed in `read_only` → **unguarded** (a warning is logged if the name reads
   like a mutation, e.g. `delete_*` — your call, but a visible one).
3. The server annotates it `destructiveHint: true` → **destructive**. A hint
   that *raises* protection is always honoured.
4. With `trust_annotations: true` only:
    - `readOnlyHint: true` and a name that doesn't read as a mutation → **unguarded**;
    - `destructiveHint: false` → **confirm**.
5. Anything else → **destructive** — the MCP specification's own default for
   `destructiveHint`.

What each level means in practice:

| Level | RBAC | Autonomy L2 / L3 | Confirmation |
|---|---|---|---|
| unguarded | viewer | allowed | none |
| confirm | operator | blocked / allowed | approval card |
| destructive | admin | blocked / blocked | approval card |

Undeclared tools therefore start out admin-only and approval-gated. Classify a
tool to make it *cheaper* to use; nothing you leave out becomes more permissive.

!!! warning "Annotations are the server describing itself"
    `trust_annotations` lets a server lower the protection on its own tools.
    Turn it on for servers you operate or have reviewed — not for a third-party
    endpoint whose tool list can change under you.

## A server's text is screened and pinned

A tool's description and schemas become part of the model's prompt, and the
server can change them whenever it is asked for its tools. Guard levels don't
depend on that text, but the model's behaviour does, so each exposed tool is
also:

- **screened**: if its description or any text in its schemas matches the
  prompt-injection patterns, the tool is withheld;
- **pinned**: its description, schemas and annotations are fingerprinted when
  first seen. If they later change (a "rug pull", or a `readOnlyHint` flipped
  to lower the guard), the tool is withheld until Orrery restarts. A restart
  pins whatever the server then serves, so review the change on the server
  first.
- **pinned by you**, optionally: first sight logs each tool's fingerprint
  (`MCP tool github_list_issues first seen, fingerprint …`). Copy the ones you
  reviewed under `pinned:` and the tool is checked against *that* value, in
  every process, from its first listing, so a server that changed between
  your review and a restart is caught too. The fingerprint covers the
  annotations as the MCP SDK spells them, so **re-pin after an MCP SDK major
  upgrade**: until you do, pinned tools are withheld (it fails closed).

Each withheld tool is logged once and audited as `mcp_tool_withheld`.

## Where the client connects

An HTTP server is a URL called with credentials, from inside your network. The
client is held to what you configured:

- **one origin**: requests go only to the configured scheme, host and port.
  The SSE transport takes its POST endpoint from the server, and the SDK
  follows redirects; anything pointing elsewhere is refused, and redirects are
  not followed at all.
- **public addresses**: every request checks what the host resolves to, and
  refuses a private, loopback or otherwise non-public address. IPv4 carried in
  IPv6 (mapped, NAT64 `64:ff9b::/96`, IPv4-compatible) is checked as the IPv4
  address it reaches. A server on your own network needs
  `allow_private_network: true`; a private IP written in the URL fails startup
  without it. A loopback URL (local development) may reach loopback, and
  nothing else private.
- **never metadata**: link-local addresses (`169.254.169.254`, `fe80::/10`)
  and cloud metadata and credential endpoints (AWS `fd00:ec2::/32`, Alibaba
  `100.100.100.200`) are refused even with
  `allow_private_network`, so opening the private network for one internal
  server doesn't also open the instance's credentials to a DNS change.
  With the opt-in, every request still resolves the name to run this check.
- **no proxy**: the guarded client ignores `HTTPS_PROXY`/`HTTP_PROXY`. A proxy
  resolves the name itself, out of reach of the address check. If your egress
  must go through a proxy, MCP over HTTP needs a direct route to the server.

!!! note "Residual: DNS rebinding"
    The check resolves the name, then the connection resolves it again, so a
    DNS server answering differently in between is not caught. Where that
    matters, configure the server by IP address (with `ca_file` if its
    certificate names the IP): a literal is checked exactly.

## Calls have a deadline

A tool call that hasn't answered within `call_timeout_seconds` is abandoned and
the model gets `{"status": "error", "error_type": "timeout"}`, telling it the
call may still complete on the server, so it checks before retrying a
mutation. The circuit breaker counts it as a failure, as it does a result the
server flagged `isError`.

## Per-user credentials

By default every call to a server uses the shared token from
`headers_from_env`, so the server sees Orrery, not the person asking. With
`auth`, each call carries a token issued for *that user*, so the server's own
permissions and audit apply to them:

```yaml
  - name: github
    transport: streamable_http
    url: https://mcp.github.example.com/mcp
    auth:
      type: token_exchange
      audience: github-mcp        # the audience the IdP issues tokens for
      scope: "issues:read"        # optional
    tools: [list_issues, get_issue]
    read_only: [list_issues, get_issue]
```

The signed-in caller's token is exchanged at your identity provider
([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693), see
[ADR-004](../adr/004-user-token-exchange.md)) for a short-lived token with the
server's audience. The caller's own token is never forwarded: it is scoped to
Orrery, and a server holding it could replay it against Orrery.

- **Who has a token:** console users and A2A agents acting for a user (a
  delegated token). Slack, Google Chat, the triage sweep and service
  principals hold none, so the server offers them **no tools**, rather than
  falling back to a shared credential.
- **Fail-closed:** a refused exchange answers the call with
  `status: access_denied`, which the circuit breaker ignores; an unreachable
  identity provider is `status: error`, which it counts.
- **Isolation:** sessions and tool listings are pooled by header, so each
  user's token gets its own MCP session; one session never carries two
  people's tokens.
- **Cache:** exchanged tokens stay in memory, per caller token, audience and
  scope, until 30 s before they expire. Concurrent calls share one exchange.

Settings, required once any server uses `auth` (startup fails without them):

| Variable | Description |
|---|---|
| `ORRERY_TOKEN_EXCHANGE_URL` | The IdP token endpoint (https; plain http only on loopback). Keycloak: `https://<host>/realms/<realm>/protocol/openid-connect/token`. |
| `ORRERY_TOKEN_EXCHANGE_CLIENT_ID` | A confidential client allowed to exchange tokens. |
| `ORRERY_TOKEN_EXCHANGE_CLIENT_SECRET` | Its secret. |

On the IdP side, allow that client to exchange tokens issued to the console
(and A2A) audiences for each server's `audience`, and nothing more.

## What else applies

Tool results from MCP servers pass through the same plugins as any other tool:
prompt-injection screening, credential redaction, the output-size cap, audit
logging, the circuit breaker and per-run budgets. In the console, MCP tool calls
appear in the live trace under their specialist.

## Deployment notes

- **Credentials are static per server** unless it sets `auth`: every
  user's call then uses the token in `headers_from_env`. See
  [Per-user credentials](#per-user-credentials).
- **stdio servers run as child processes of the agent**, one per specialist.
  For anything long-lived or shared, prefer a remote server over
  `streamable_http`.
- **Helm:** declare servers under `mcp.servers` (or point
  `mcp.existingConfigMap` at a ConfigMap with an `mcp.yaml` key) and set
  `mcp.enabled: true`. The chart renders the file into a ConfigMap, mounts it
  read-only at `mcp.mountPath`, sets `ORRERY_MCP_CONFIG`, and rolls the pods
  when it changes. Put the env vars named in `headers_from_env` in `secrets:` /
  `existingSecret`, which the pod already loads. Enabling MCP with no servers
  fails the render. The published image includes the `[mcp]` extra.

  ```yaml
  secrets:
    GITHUB_MCP_AUTHORIZATION: "Bearer <token>"   # or via existingSecret
  mcp:
    enabled: true
    servers:
      - name: github
        description: GitHub issues and pull requests for the platform repos
        transport: streamable_http
        url: https://api.githubcopilot.com/mcp/
        headers_from_env: {Authorization: GITHUB_MCP_AUTHORIZATION}
        tools: [list_issues, get_issue]
        read_only: [list_issues, get_issue]
  ```
