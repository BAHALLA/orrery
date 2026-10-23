# Security & Authentication

This page covers the authentication layer that gates the HTTP front door
(`orrery_core.serving.server`) and the secrets-management seam that keeps API
keys out of environment variables.

The Slack and Google Chat transports authenticate at their own layer
(Slack signing-secret + Google OIDC tokens) and are unaffected by the
settings here.

---

## Threat model

| Threat | Mitigation |
|--------|------------|
| Anyone on the network calling `/chat` as `admin` | `AuthPlugin` only accepts a role from a verified JWT (`_auth` payload). Missing payload → forced `viewer`. |
| Stolen/replayed token | `exp` is required; default leeway 30 s. Bind to `aud` + `iss` to prevent reuse against other services. |
| Token forgery | HS256/384/512 verify against a shared `JWT_SECRET`, which must be at least as long as the hash (32/48/64 bytes, RFC 7518 §3.2) or the server refuses to start; RS256/ES256 verify against the IdP's public keys via JWKS. A token whose header `alg` differs from `JWT_ALGORITHM` is refused before any key lookup (algorithm confusion). |
| Forged tokens used to hammer the IdP / stall the server | JWKS verification runs on a worker thread, never the event loop. The key set is cached for 10 minutes; a token with an unknown `kid` can force a refresh at most once per 60 s (enough to pick up a key rotation), and every other unknown `kid` in that window is rejected from cache. Tokens with no `kid` never reach the network. |
| Script injection in the web console (token theft) | The console is served under a strict Content-Security-Policy: own-origin scripts and styles only, no inline script or `eval`, `object-src 'none'`, `base-uri 'none'`, `frame-ancestors 'none'`. Model output is rendered without raw HTML. Every response also carries `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and COOP, and JSON is `no-store`. |
| Rate limits bypassed by spreading requests over replicas | `ORRERY_RATE_LIMIT_STORAGE_URI` shares the counters across replicas (e.g. Redis). A multi-replica deployment on the in-memory default logs that limits are per replica. |
| IdP outage | Reported as `503 Retry-After: 5`, not `401` — the caller's token may be perfectly valid, and a 401 tells the console to discard it. |
| Leaked secrets via env vars | `ORRERY_SECRETS_DIR` reads secrets from a mounted Kubernetes Secret volume so they never appear in the pod's environment. |
| Privilege escalation via untrusted session state | `set_user_role()` flags the role as server-trusted; `ensure_default_role()` forces `viewer` on any session that didn't go through the trusted path. That flag lives in the same state as the role, so `IdentityStateGuardPlugin` (first in `default_plugins()`) makes sure **no tool call can write either**. It snapshots the identity and authorization keys (`_auth`, `user_role` and its lock, the autonomy level and its lock, `actor`, the confirmation mode and decision) before every tool call and reverts any change afterwards, even if the tool raised. The only exception is clearing a consumed decision. Every reverted write is logged and audited as `protected_state_write_reverted`. |
| Stale role after revocation | The HTTP front door re-stamps `_auth` on every request from the verified token, so a re-minted token with a downgraded role applies on the next call. |

---

## Configuration

### Settings

The HTTP front door (`ServerConfig.from_env()`, `JWTConfig.from_env()`) reads:

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_ENABLED` | `true` | Verify a bearer JWT on every request. `false` makes every caller an anonymous **viewer** (local try-outs only). With it on, the JWT settings below must be valid or the server refuses to start. |
| `JWT_ALGORITHM` | `HS256` | `HS256`/`HS384`/`HS512` (shared secret) or `RS*`/`ES*` (JWKS). |
| `JWT_SECRET` | unset | Shared secret for `HS*`; at least 32/48/64 bytes. |
| `JWT_JWKS_URL` | unset | The IdP's JWKS endpoint, required for `RS*`/`ES*`. |
| `JWT_AUDIENCE` | unset | Expected `aud`. Recommended in production. |
| `JWT_ISSUER` | unset | Expected `iss`. Recommended in production. |
| `JWT_ROLE_CLAIM` | `roles` | Claim holding the roles; may be a dotted path (see [Role mapping](#role-mapping)). |
| `JWT_LEEWAY_SECONDS` | `30` | Clock-skew allowance on `exp`/`nbf`. |
| `ORRERY_WEB_CONSOLE_ENABLED` | `false` | Serve the built web console. |
| `ORRERY_DOCS_ENABLED` | on only when auth is off | Serve the interactive `/docs` schema. |
| `ORRERY_CONSOLE_OIDC_ISSUER`, `_CLIENT_ID`, `_SCOPE`, `_ROLE_CLAIM` | unset | Console SSO, served at runtime. The issuer must match `JWT_ISSUER`. See [Web console](../integrations/web-console.md). |
| `ORRERY_CORS_ORIGINS` | unset | Comma-separated origins allowed to call the API from a browser. |
| `ORRERY_CSP_EXTRA_ORIGINS` | unset | Origins the console may connect to besides its own and `JWT_ISSUER`'s. |
| `ORRERY_HUMAN_CLIENTS` | unset | Clients people sign in through. See [People, agents and services](#people-agents-and-services-adr-006). |
| `ORRERY_SERVICE_CLIENTS` | unset | `client=role` ceilings for machine clients ([ADR-006](../adr/006-caller-principals.md)). |
| `ORRERY_PROTECTED_NAMESPACES` | unset | Comma-separated namespace globs where only admins may mutate ([ADR-001](../adr/001-rbac.md)). Reads are never scoped. |
| `ORRERY_AUTONOMY_LEVEL` | unset | `L2`, `L3` or `L4`. See [Autonomy levels](#autonomy-levels). |

### Minimal HS256 setup (single-binary deployments)

```bash
AUTH_ENABLED=true
JWT_ALGORITHM=HS256
JWT_SECRET=$(openssl rand -hex 32)    # 32+ bytes for HS256 (48 for HS384, 64 for HS512)
JWT_AUDIENCE=orrery
JWT_ISSUER=https://your-gateway
```

Issue tokens from your gateway (or `jwt.io` for testing) with at least:

```json
{
  "sub": "alice@example.com",
  "aud": "orrery",
  "iss": "https://your-gateway",
  "exp": 1234567890,
  "roles": ["operator"]
}
```

### Production RS256/JWKS setup (IdP-fronted deployments)

```bash
AUTH_ENABLED=true
JWT_ALGORITHM=RS256
JWT_JWKS_URL=https://your-tenant.auth0.com/.well-known/jwks.json
JWT_AUDIENCE=https://orrery.your-org.com
JWT_ISSUER=https://your-tenant.auth0.com/
JWT_ROLE_CLAIM=https://orrery.your-org.com/roles    # Auth0 custom claim
```

Common JWKS endpoints:

| Provider | URL pattern |
|----------|-------------|
| Auth0 | `https://YOUR_TENANT.auth0.com/.well-known/jwks.json` |
| Keycloak | `https://KC/realms/REALM/protocol/openid-connect/certs` |
| Okta | `https://YOUR_ORG.okta.com/oauth2/default/v1/keys` |
| Google IAP | `https://www.googleapis.com/oauth2/v3/certs` |
| GitHub OIDC (CI tokens) | `https://token.actions.githubusercontent.com/.well-known/jwks` |

---

## Role mapping

`extract_role()` reads the `JWT_ROLE_CLAIM` claim and returns one of
`viewer` / `operator` / `admin` using these rules:

1. The claim may be a list (`["operator", "billing"]`) or a single
   space/comma-separated string (`"operator,billing"`).
2. Any token matching `admin` (or the aliases `orrery-admin`,
   `orrery_admin`) returns `admin`. Matching ignores case.
3. Otherwise, any token matching `operator` (or `orrery-operator`,
   `orrery_operator`) returns `operator`.
4. Otherwise, `viewer`.

`JWT_ROLE_CLAIM` may be a **dotted path** into nested claims, because the
providers people actually deploy nest their roles — Keycloak uses
`realm_access.roles` for realm roles and `resource_access.<client>.roles` for
client roles, neither of which a flat lookup reaches:

```bash
JWT_ROLE_CLAIM=realm_access.roles
```

A path that doesn't resolve is treated as "no roles" and yields `viewer` — it
fails closed. The console reads the same claim for its role badge
(`ORRERY_CONSOLE_OIDC_ROLE_CLAIM` defaults to `JWT_ROLE_CLAIM`).

Custom role names are supported by passing `admin_values=` /
`operator_values=` when calling `extract_role` directly. The HTTP path
uses the defaults and has no setting for other names, so map your IdP's
roles onto the names above in the IdP. (`RolePolicy` is a different
thing: it sets the minimum role per *tool*.)

---

## Autonomy levels

RBAC decides *who* may call a tool; the autonomy level decides what this
*process* may do at all, whoever asks. `AutonomyPlugin` reads
`ORRERY_AUTONOMY_LEVEL`:

| Level | Allows |
|-------|--------|
| `L2` | Read-only. Only unguarded tools run; every `@confirm`/`@destructive` tool is refused. |
| `L3` | Mutations, but `@destructive` tools are refused. |
| `L4` | Everything, with `@destructive` tools behind a confirmation. |

Unset (or `off`/`none`), no process-wide level applies; the plugin still
pins service principals to `L2` per request ([ADR-006](../adr/006-caller-principals.md)).
An unrecognised value is treated as `L2`. A refused call returns
`{"status": "BLOCKED"}`. Per-tool exceptions (an L2 whitelist, an L3
blacklist) are code-only: `default_plugins(autonomy_l2_whitelist=…,
autonomy_l3_blacklist=…)`. Pin unattended runs such as the triage sweep
to `L2` unless you want them to act.

---

## People, agents and services ([ADR-006](../adr/006-caller-principals.md))

A guarded action waits for a human decision, and only the person who raised it
may approve it. That only holds if the caller is a person. Orrery classifies
every verified token:

| Kind | Recognised by | Role | Can approve |
|---|---|---|---|
| **human** | Its client (`azp`, else `client_id`) is in `ORRERY_HUMAN_CLIENTS` | From the role claim | Its own actions |
| **delegated** | A verified RFC 8693 `act` claim (an agent acting for a user) | The lower of the user's role and the agent's ceiling | Never in the agent's messages; the user approves out of band |
| **service** | Anything else, or a client in `ORRERY_SERVICE_CLIENTS` | The client's ceiling (default `viewer`), and pinned to autonomy **L2** | Never |

```bash
ORRERY_HUMAN_CLIENTS=orrery-console,orrery-cli          # clients people sign in through
ORRERY_SERVICE_CLIENTS=ci-bot=viewer,partner-agent=operator  # machine clients and their ceilings
```

!!! warning "Set `ORRERY_HUMAN_CLIENTS` in production"
    Unset, every console token counts as a person (the behaviour before
    ADR-006), so a client-credentials token with the operator role could
    approve its own action. The server logs a warning at startup.

A machine caller's message is never recorded as a decision, whatever it
says. To approve an action an agent raised for you, use the console or:

```bash
curl -X POST https://orrery.example.com/confirmations/<action_id>/decision \
  -H "Authorization: Bearer $YOUR_TOKEN" -d '{"decision": "approve"}'
```

Approve works only for your own action (anything else is a 404, the same as an
unknown id); deny is open to anyone. The approval applies when the agent
retries the same call with the same arguments, within two minutes.

---

## Wiring the AuthPlugin

`AuthPlugin` consumes the `_auth` payload the HTTP front door stashes
into session state. Enable it on the plugin stack:

```python
from orrery_core import default_plugins
from orrery_core.serving.server import ServerConfig, create_app

plugins = default_plugins(enable_auth=True)
app = create_app(
    root_agent=root_agent,
    app_name="orrery",
    plugins=plugins,
    config=ServerConfig.from_env(),
)
```

With `enable_auth=True` and `require_auth=True` (default), a session
that reaches the agent without a verified `_auth` payload has its role
forced to `viewer` and a warning is logged. This makes privilege
escalation impossible even if a future transport forgets to wire the
auth dependency.

---

## Secrets management

`SecretsManager` resolves keys in this order:

1. Explicitly registered backends (e.g. Vault adapter).
2. `FileBackend(ORRERY_SECRETS_DIR)` — auto-installed when the env var
   points at a real directory.
3. Environment variables.
4. The caller's `default` (or `None`).

Recommended Kubernetes pattern:

```yaml
volumeMounts:
  - name: orrery-secrets
    mountPath: /var/run/secrets/orrery
    readOnly: true
env:
  - name: ORRERY_SECRETS_DIR
    value: /var/run/secrets/orrery
volumes:
  - name: orrery-secrets
    secret:
      secretName: orrery-secrets   # contains JWT_SECRET, GOOGLE_API_KEY, …
```

Each key in the `Secret` becomes a file under
`/var/run/secrets/orrery/`. Application code stays the same — read
through `default_secrets.get("JWT_SECRET")` rather than
`os.getenv("JWT_SECRET")` to pick up the volume transparently.

---

## Runtime hardening plugins (AEP-013)

Three content-level defenses ship in `default_plugins()`, all **on by
default** with env off-switches:

### Prompt-injection screening

`SafetyScreenPlugin` runs before the model does (`before_run_callback` — the
one plugin hook that can halt the runner). Messages matching overt override
phrasings ("ignore previous instructions", "reveal your system prompt",
"bypass the guardrails", ...) are refused before they cost a token or reach a
tool; the attempt is logged for audit visibility. The patterns are
deliberately narrow — an SRE pasting incident logs must not trip false
positives — and this is a *baseline*: the real security boundary remains the
deterministic layer underneath (RBAC, confirmations, autonomy gates, input
validation), which holds even when a novel paraphrase gets past the screen.

```bash
ORRERY_SAFETY_SCREEN=false   # disable (default: on)
```

### PII / secret redaction of tool outputs

`PIIRedactionPlugin` scrubs credentials from every tool result before the
model, the session store, **and the audit log** see them (it registers ahead
of `AuditPlugin`). Two layers: dict keys that name a credential
(`password`, `access_token`, `api_key`, ...) have their values replaced
outright, and string values are scanned for secret shapes (`password=...`
pairs in log lines, PEM private-key blocks, AWS/GitHub/Slack/OpenAI token
prefixes, JWTs). Pagination cursors (`next_page_token`) are allowlisted.

IP addresses are **not** redacted by default — an SRE agent that cannot see
pod or broker IPs cannot diagnose much. Compliance-bound deployments can opt
in:

```bash
ORRERY_PII_REDACTION=false   # disable credential scrubbing (default: on)
ORRERY_REDACT_IPS=true       # also redact IPv4 addresses (default: off)
```

### Gemini content-safety filters

`create_agent()` attaches `GenerateContentConfig` safety settings (dangerous
content, harassment, hate speech, sexually explicit) to every Gemini model.
The default threshold is `BLOCK_ONLY_HIGH`: SRE conversations legitimately
talk about killing pods and destroying volumes all day, and stricter
thresholds trip on that. LiteLLM-routed providers (Claude, OpenAI, Ollama)
do not consume the genai config and are unaffected.

```bash
GEMINI_SAFETY_FILTERS=false                     # disable (default: on)
GEMINI_SAFETY_THRESHOLD=BLOCK_MEDIUM_AND_ABOVE  # default: BLOCK_ONLY_HIGH
```

---

## Acting as the user (impersonation)

Kubernetes and Elasticsearch calls can carry the verified requester, so those systems'
own permissions apply too. Full guide: [Acting as the user](../impersonation.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_IMPERSONATION` | unset | `kubernetes`, `elasticsearch`, or both. Unset: everything runs as the service account. A typo stops startup. |
| `ORRERY_IMPERSONATE_USER_CLAIM` | `email` | Claim naming the user (dotted path). `email` is used only when `email_verified` is `true`; otherwise the subject is used. |
| `ORRERY_IMPERSONATE_GROUPS_CLAIM` | `groups` | Claim listing the user's groups (dotted path). |
| `ORRERY_IMPERSONATE_K8S_USER_PREFIX` | (none) | Prepended to the impersonated Kubernetes user; match the API server's `--oidc-username-prefix`. |
| `ORRERY_IMPERSONATE_K8S_GROUP_PREFIX` | (none) | Prepended to each impersonated Kubernetes group; match `--oidc-groups-prefix`. A `system:` user or group is always refused. |
| `ORRERY_IMPERSONATION_FALLBACK` | unset | `service` uses the service account when no user is known. A rollout aid; leave unset afterwards, so unknown callers are refused. |

## Per-user tokens (token exchange)

MCP servers with `auth: {type: token_exchange}` receive a token the identity
provider issued for the requesting user (RFC 8693, [ADR-004](../adr/004-user-token-exchange.md)).
Full guide: [MCP servers](../integrations/mcp.md#per-user-credentials).

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_TOKEN_EXCHANGE_URL` | unset | IdP token endpoint. https only (http on loopback). Required once a server uses `auth`. |
| `ORRERY_TOKEN_EXCHANGE_CLIENT_ID` | unset | Confidential client allowed to exchange tokens. |
| `ORRERY_TOKEN_EXCHANGE_CLIENT_SECRET` | unset | Its secret. |

## What this does **not** cover (intentionally)

- **Vault / GCP Secret Manager / AWS Secrets Manager adapters** —
  `SecretsManager` exposes the `SecretsBackend` protocol; concrete
  adapters will ship as separate modules so each provider's SDK is
  optional.
- **Supply-chain security** (image signing, SBOMs, admission policy) — see
  [Supply Chain Security](../supply-chain.md).
