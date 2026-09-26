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
   `orrery_admin`) returns `admin`.
3. Otherwise, any token matching `operator` returns `operator`.
4. Otherwise, `viewer`.

`JWT_ROLE_CLAIM` may be a **dotted path** into nested claims, because the
providers people actually deploy nest their roles — Keycloak uses
`realm_access.roles` for realm roles and `resource_access.<client>.roles` for
client roles, neither of which a flat lookup reaches:

```bash
JWT_ROLE_CLAIM=realm_access.roles
```

A path that doesn't resolve is treated as "no roles" and yields `viewer` — it
fails closed. Set the console's `VITE_OIDC_ROLE_CLAIM` to the same value, or its
badge will disagree with the role the server enforces.

Custom role names are supported by passing `admin_values=` /
`operator_values=` when calling `extract_role` directly. The HTTP path
uses the defaults.

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

## What this does **not** cover (intentionally)

- **Vault / GCP Secret Manager / AWS Secrets Manager adapters** —
  `SecretsManager` exposes the `SecretsBackend` protocol; concrete
  adapters will ship as separate modules so each provider's SDK is
  optional.
- **Supply-chain security** (image signing, SBOMs, admission policy) — see
  [Supply Chain Security](../supply-chain.md).
