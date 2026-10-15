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

## What else applies

Tool results from MCP servers pass through the same plugins as any other tool:
prompt-injection screening, credential redaction, the output-size cap, audit
logging, the circuit breaker and per-run budgets. In the console, MCP tool calls
appear in the live trace under their specialist.

## Deployment notes

- **Credentials are static per server.** Every user's call uses the token in
  `headers_from_env`. Acting with each user's own identity is the next step on
  the roadmap (per-user OAuth / token exchange).
- **stdio servers run as child processes of the agent**, one per specialist.
  For anything long-lived or shared, prefer a remote server over
  `streamable_http`.
- On Kubernetes, mount the config file and the header secrets the way the
  chart mounts other secrets, and set `ORRERY_MCP_CONFIG` to the mounted path.
