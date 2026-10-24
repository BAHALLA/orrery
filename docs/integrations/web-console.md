# Web console

A single-page operator console served by the same FastAPI front door as the API
(AEP-019). It is a **transport over `AgentGateway`** — the same pipeline Slack
and Google Chat use — not a second implementation of the agent. No agent,
plugin, or guardrail logic changes when the console changes.

It is **off by default**. A browser-facing chat surface amplifies any auth gap,
so it only appears when you deliberately turn it on.

## What it is for

`adk web` is a *developer* debugger: raw events, function-call payloads, session
plumbing. This console is the *operator* surface — hold a conversation, see
which specialist ran which tool, approve a guarded action, and check whether the
environment is actually wired. Both coexist; neither replaces the other.

## Enabling it

```bash
make run-api          # builds the bundle and serves it at http://localhost:8000
```

That is `make web-build` (Vite → `serving/static/`) plus `make run-api`.
Mint a token in another terminal and paste it into the console:

```bash
make dev-token ROLE=operator
```

In a deployment, set:

| Variable | Default | Purpose |
|---|---|---|
| `ORRERY_WEB_CONSOLE_ENABLED` | `false` | Serve the bundle at `/`. Off unless set. |
| `ORRERY_CORS_ORIGINS` | *(empty)* | Only needed when the console is served from a different origin than the API. Never `*` — a wildcard disables credentialed CORS (with a warning). |
| `ORRERY_CHAT_RATE_LIMIT` | `30/minute` | Per-caller ceiling on `POST /chat`. |
| `ORRERY_SELFTEST_RATE_LIMIT` | `10/minute` | Per-caller ceiling on the environment check. |
| `ORRERY_RATE_LIMIT_STORAGE_URI` | `memory://` | Where rate-limit counters live. In memory, each replica counts separately, so N replicas allow N× the limit (logged at startup). Point every replica at one store, e.g. `redis://redis:6379` (needs the `redis` package), to enforce the limit per caller. An unknown scheme fails at startup. |
| `ORRERY_CSP_EXTRA_ORIGINS` | *(empty)* | Comma-separated origins the console may `connect`/`frame` besides its own. The `JWT_ISSUER` origin is allowed automatically for SSO. Needed only for a console built with `VITE_API_BASE_URL` on another origin. |

### Security headers

Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin` and a
restrictive `Permissions-Policy`. JSON responses are `Cache-Control: no-store`
because they carry transcripts and identity. The console is served under a
Content-Security-Policy that allows only its own scripts and styles: no inline
script, no `eval`, no CDN, `frame-ancestors 'none'`. The console keeps a bearer
token in the browser, so this is what makes a future injection sink harmless
instead of a token theft. It was verified in a real browser against the built
bundle, including the chat view's markdown and syntax highlighting (zero
violations). `/docs` and `/redoc`, which load from a CDN and are served only when
enabled, are exempt from the CSP. Set HSTS at your TLS-terminating ingress.

The Docker image builds the bundle in a `node:` stage and copies it into the
Python runtime, so the runtime image stays Node-free.

## Authentication

The console holds no trust of its own. The static shell carries no secrets; a
bearer token rides the `Authorization` header on every API call, and the server
verifies it, resolves the role via RBAC, and enforces everything — the console's
role badge is decoration.

There are two ways to obtain that token. Which one applies is deployment
configuration, and the console renders only the matching sign-in surface.

### SSO (recommended)

Set `ORRERY_CONSOLE_OIDC_ISSUER` on the server and the console runs **OpenID
Connect Authorization Code with PKCE**. It is provider-agnostic: the same image
works against Keycloak, Authentik, Auth0, Okta, Entra ID or Google, and it uses
a *public* client with no secret, which is the correct pattern for a browser SPA.

The settings are read **at runtime**: the front door serves them at
`/console-config.js`, which the console loads before it starts. The published
image therefore needs no rebuild to turn SSO on.

| Server variable | Default | Description |
|---|---|---|
| `ORRERY_CONSOLE_OIDC_ISSUER` | — | Issuer URL. **Unset disables SSO** and falls back to the token gate. |
| `ORRERY_CONSOLE_OIDC_CLIENT_ID` | `orrery-console` | Public client id |
| `ORRERY_CONSOLE_OIDC_SCOPE` | `openid profile email` | Requested scopes |
| `ORRERY_CONSOLE_OIDC_ROLE_CLAIM` | `JWT_ROLE_CLAIM` | Dotted path to the roles claim; it **must match the server's `JWT_ROLE_CLAIM`**, which is why it defaults to it |

The same four settings can still be baked into a custom build as
`VITE_OIDC_ISSUER`, `VITE_OIDC_CLIENT_ID`, `VITE_OIDC_SCOPE` and
`VITE_OIDC_ROLE_CLAIM`. When both are set, the runtime value wins. The issuer's
origin is added to the console's `connect-src`/`frame-src` automatically.

The server side needs no new code — point it at the provider's JWKS:

```bash
JWT_ALGORITHM=RS256
JWT_JWKS_URL=https://idp.example.com/realms/orrery/protocol/openid-connect/certs
JWT_AUDIENCE=orrery-console
JWT_ISSUER=https://idp.example.com/realms/orrery
JWT_ROLE_CLAIM=realm_access.roles
```

The access token is held **in memory** and renewed silently, so it is not
sitting in `localStorage` for any script on the origin to read. Signing out ends
the provider session too — a local-only sign-out would silently sign the same
user straight back in, which is not what anyone means by signing out of a shared
machine.

!!! note "The redirect URI is the console root"
    Not `/auth/callback`. The front door serves the bundle with
    `StaticFiles(html=True)`, which 404s unknown deep paths — a sub-path
    callback works under the Vite dev server and breaks in production. Register
    `https://your-console/` (and `http://localhost:5173/` for dev) with your
    provider.

### Pasted token (default, and for CI)

With `ORRERY_CONSOLE_OIDC_ISSUER` (and `VITE_OIDC_ISSUER`) unset the console shows the token gate: paste a JWT,
e.g. from `make dev-token`. This keeps CI, offline work, and quick local runs
usable with no identity provider running.

!!! warning "A pasted token lives in `localStorage`"
    That makes it readable by any script running on the same origin. The console
    ships no third-party scripts and renders agent replies through
    `react-markdown` (React elements, never `dangerouslySetInnerHTML`), but
    treat pasted tokens as short-lived and scope them to the role the operator
    actually needs. SSO mode does not have this exposure.

Signing out clears the token and any keys an earlier build wrote. Transcripts
are no longer among them: history lives in the session store and is fetched with
the signed-in user's own token, so a shared browser can only ever show the
current user's conversations.

### Trying SSO locally

A throwaway Keycloak ships under the `sso` compose profile:

```bash
make up PROFILES=sso  # Keycloak on :8081, realm "orrery" pre-imported
make run-api SSO=1    # builds the console with SSO, serves it on :8000
```

Three demo users, password same as username: `viewer`, `operator`, `admin` —
enough to see the RBAC tiers behave differently in the browser. `make down`
stops it.

!!! danger "Two gotchas that produce a generic 'Invalid or expired token'"
    Both bite everyone integrating Keycloak, and neither is obvious from the
    error:

    1. **Roles are nested.** Keycloak puts realm roles at `realm_access.roles`,
       not a flat `roles` claim. Set `JWT_ROLE_CLAIM=realm_access.roles` (the console
       follows it) or every user silently resolves to
       `viewer`. Dotted paths are supported on both sides.
    2. **The audience is wrong by default.** Keycloak issues access tokens with
       `aud: account`. Add an **audience mapper** on the client emitting your
       client id, or leave `JWT_AUDIENCE` unset. The bundled realm includes the
       mapper.

    The bundled realm also enables direct access grants so the chain can be
    checked with `curl`. The browser never uses them, and the realm is a
    throwaway local fixture — do not reuse it anywhere real.

## What you get

### Streaming answers with a live trace

Each turn is sent to `POST /chat/stream` and narrated as it runs (AEP-009).
Above the answer, a live trace shows which specialists are being consulted and
each one's own tool calls as they start and finish, with durations and outcome:
✓ done, ⚠ failed, 🔒 waiting for approval or refused. That includes calls made
*inside* a specialist, which the Runner's own event stream cannot show. The
answer then streams in as the model writes it. When the turn ends, the trace
folds into a one-line summary ("Kafka, Kubernetes · 4 tool calls · 7.2s") that
expands on click.

The trace carries **names and outcomes only**. Tool arguments and results are
never streamed to the browser: they are attacker-reachable text and can hold
credentials.

The console falls back cleanly against an older server. A `404` on
`/chat/stream` retries the turn on `POST /chat`.

### Tool-call timeline

The **Tool calls** tab lists every recorded call — which specialist, which tool,
which arguments, what status — from the session's activity log. It is the full
record, where the live trace is the at-a-glance view.

A turn can be **stopped**. A triage sweep fans out to five specialists and can
run for a minute; the Stop button abandons the wait. The server keeps working —
this stops the client waiting, it does not cancel the run. Whatever had already
streamed stays on screen, and reopening the conversation shows the final answer.

### Layout and themes

The conversation is a centred reading column. Answers render full width as
documents, because they are reports with tables and code. On narrow screens the
history becomes a slide-out menu and the inspector a drawer, so the console
works on a phone. A light / system / dark switch sits in the sidebar footer and
is remembered per browser. `/` focuses the composer, and `Esc` closes an open
menu or drawer.

A new conversation opens on a few starting questions, one per specialist, plus
a full health sweep.

A failed turn offers **Retry**, which replays the same message without
duplicating it in the transcript. A rate-limited turn says so, with the wait.

### Approve / deny for guarded tools

When a `@confirm` or `@destructive` tool is gated, the console renders the
pending action and two buttons. The buttons send the literal decision words
through `POST /chat` — **the frontend decides nothing**. The
requester-verified gate is the only authority on who may approve, and it
refuses a decision from anyone but the requester, or one that predates the
action it would authorize.

### Conversation history that follows the user

The sidebar is a view of the **session store**, not of the browser. On load the
console asks `GET /sessions` for the caller's conversations and fetches a
transcript (`GET /session/{id}`) only for the one it opens, so a long history
costs one request. Deleting a conversation deletes the session.

Consequences worth knowing, since the console used to keep this in `localStorage`:

- History is the same on every machine, and clearing site data loses nothing.
  Previously the sidebar was the only index of which sessions belonged to whom,
  so a different browser left the transcripts unreachable in the store.
- Long threads are no longer truncated. The browser copy was capped at 200
  messages per conversation, which made the visible transcript disagree with
  what the agent still remembered.
- Titles come from the server, written into session state on a conversation's
  first turn. A session opened by another transport (Slack, the CLI) has none
  and renders as "New chat".

!!! note "In-memory deployments have no history to list"
    Without `DATABASE_URL` the session store is per-process and empty at startup,
    so the sidebar starts blank on every restart. That is the same trade-off
    every other stateful feature makes — see the fail-fast behaviour in
    `persistence/db.py`.

### Triage view

**Run triage** sends a canned prompt to the `incident_triage_agent`. The result
renders as a severity banner (healthy / degraded / critical) from the recorded
`incident_severity` — the machine-readable verdict, not a parse of the prose —
with the full report below and a chip per system that was actually consulted.

!!! note "Chips are derived from tool calls, not from the report text"
    A system that was never called shows no chip. "We didn't ask" and "healthy"
    are different answers, and the console must not blur them.

### System tab — the environment check

The single most useful thing here for a new user. **Check my environment** runs,
concurrently and read-only:

- a **one-token round-trip** against the configured provider and model, and
- each specialist's **cheapest read-only tool** (Kafka cluster health, K8s
  cluster info, Elasticsearch cluster health, Prometheus targets, `docker ps`).

Each row is green or red with the reason and, when red, **what to configure**.
This exists because nearly every first-run failure is "a credential or endpoint
was never wired" — and the only feedback used to be a stack trace buried in a
tool result several turns into a conversation.

The tab also shows the **server-resolved** role and the **active autonomy
level** (L2/L3/L4), so a viewer understands up front why mutating tools are
unavailable rather than discovering it when one is refused.

```bash
# The same check over the API
curl -X POST -H "Authorization: Bearer $TOKEN" localhost:8000/onboarding/selftest
```

Probes must stay read-only: the self-test does not consult RBAC, so anything
with side effects does not belong in that list. Deployments register their own
via `create_app(integration_probes=...)` — core has no dependency on any agent
package.

## Endpoints the console uses

| Endpoint | Purpose |
|---|---|
| `POST /chat/stream` | One conversation turn as Server-Sent Events: `session`, `progress`, `delta`, `message`, `error`, `done`. Same rate limit as `/chat`. |
| `POST /chat` | One conversation turn as a single JSON reply. The console's fallback, and the endpoint for scripted clients. |
| `GET /sessions` | The caller's conversations, newest first — the sidebar's history. No transcripts. |
| `GET /session/{id}` | One conversation with its transcript, rebuilt from the stored events. |
| `DELETE /session/{id}` | Delete one of the caller's conversations. |
| `GET /session/{id}/activity` | Tool-call timeline, scoped to the caller's own session. |
| `GET /session/{id}/triage` | Recorded severity + report for that session. |
| `GET /confirmations/pending` | The caller's own pending guarded action, for rendering. |
| `POST /confirmations/{action_id}/decision` | Approve or deny a pending action outside the chat turn. People only; approve only your own ([ADR-006](../adr/006-caller-principals.md)). |
| `GET /me` | Server-resolved role, active autonomy level, configured model. |
| `POST /onboarding/selftest` | Model connectivity + integration probes. |
| `GET /admin/catalog` | **Admin only.** The editable agent catalog (switched-off specialists, composed agents, version) and what it may contain, including the names a composed agent may not take (`reserved_names`). |
| `PUT /admin/catalog` | **Admin only, and only with `ORRERY_ADMIN_EDITING=true`.** Replace the catalog. 409 if it changed since the version you edited, 422 if it can't be built — in both cases nothing changes. The console keeps your draft through a 409, so saving again applies it on top of the newer version. |
| `GET /admin/agents` | **Admin only.** Every specialist the coordinator can reach: its source (built-in / plugin / declarative / MCP), entry-point key, package and version, model, whether a confirmation gate guards it, and each tool's guard level. A `deployment` block gives the Orrery, ADK and Python versions, the coordinator's model, the autonomy level, the env's specialist selection and when the process started — the **Agents** tab. |

Session-scoped endpoints pin `user_id` to the verified subject, so another
user's session id resolves to a plain 404 — indistinguishable from one that
never existed.

## Developing

```bash
make install   # npm ci
make run-web   # Vite dev server with HMR (expects the API on :8000)
make test-web  # lint + format + typecheck + tests + build (mirrors CI)
```

`web/` is a separate toolchain, deliberately outside the uv workspace: `make
test` never needs Node, and the Python contributor path stays intact. The API
contract and its consumer live in the same commit, so an endpoint change and its
frontend update land together.
