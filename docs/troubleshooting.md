# Troubleshooting

Common errors across every surface, with pointers to the deeper fix. If the symptom isn't here, `kubectl logs` / the bot logs are your friend — almost every failure path emits a structured JSON log line with enough context to bisect.

## Authentication & authorization

### `401 Missing bearer token` / `401 Invalid token` on `POST /chat`
The HTTP front door (`orrery_core.serving.server`) requires a valid JWT bearer token whenever `AUTH_ENABLED=true`. Common causes:

- **No Authorization header.** Send `Authorization: Bearer <jwt>`. `/healthz` and `/readyz` are intentionally exempt.
- **Audience / issuer mismatch.** The token's `aud` and `iss` claims must match `JWT_AUDIENCE` / `JWT_ISSUER` byte-for-byte. The server returns a generic 401 to avoid leaking which check failed — set `LOG_LEVEL=DEBUG` to see the underlying PyJWT message.
- **Token expired.** `exp` is mandatory; the default clock-skew leeway is 30s. NTP-drift is the right fix, not raising `JWT_LEEWAY_SECONDS`.
- **Stale JWKS cache (RS256).** Public keys are cached for 10 minutes per pod. If the IdP just rotated, restart the pod or wait for the next refresh.
- **`JWT_SECRET` mismatch (HS256).** The verifying secret must match what your gateway uses to sign. If you set it via `secretsVolume`, confirm `ORRERY_SECRETS_DIR/JWT_SECRET` exists in the pod (`kubectl exec ... -- ls /var/run/secrets/orrery`).

Full setup recipes: [Security & auth](config/security.md).

### `JWT_SECRET is required when JWT_ALGORITHM=HS256` at startup
Authentication is on (`AUTH_ENABLED` defaults to `true`) but no secret reached the process.

**Bare `docker run` of the image:** for a local try-out, pass `-e AUTH_ENABLED=false` (anonymous, every caller is a viewer) plus `-e ORRERY_WEB_CONSOLE_ENABLED=true` for the console, as in [Getting Started](getting-started.md#kick-the-tires-single-container-30-seconds). Otherwise pass `-e JWT_SECRET=…` (at least 32 bytes for HS256) or the `JWT_JWKS_URL` setup in [Security & auth](config/security.md).

**Helm:** the chart was rendered with `auth.enabled=true` but no secret reached the pod. Either:

- Set `JWT_SECRET` under the chart's `secrets:` block (Helm-managed), or
- Add it to the `existingSecret` you reference, or
- Enable `secretsVolume` and put `JWT_SECRET` in the mounted Secret.

`create_app()` validates the JWT config eagerly so you catch this at boot rather than on the first request.

### "User has admin role in the JWT but is denied"
Three things to check, in order:

1. The `JWT_ROLE_CLAIM` env var matches the claim your IdP actually puts roles in (Auth0 typically namespaces it as `https://YOUR_API/roles`).
2. The role value is one of `admin` / `operator` / `viewer`, or an alias: `orrery-admin`, `orrery_admin`, `orrery-operator`, `orrery_operator` (case-insensitive). The HTTP front door accepts no other names, so map your IdP's role to one of these in the IdP (a role mapper or claim rule). `extract_role(admin_values=…, operator_values=…)` takes custom names only in your own integration code. (`RolePolicy` is unrelated: it maps *tools* to minimum roles.)
3. The HTTP front door re-stamps `_auth` on every `POST /chat`, and `AuthPlugin.before_agent_callback` re-applies `set_user_role` on every turn — so the role tracks the verified token live. If you're not going through `/chat` (e.g. you're inside the Slack or Google Chat handler), the role is whatever the transport set at session creation; restart the thread to pick up env-var changes.

### `401 Unauthorized: Invalid ID token` (Google Chat)
- `GOOGLE_CHAT_AUDIENCE` must match the HTTP endpoint URL **byte-for-byte**, including the trailing slash. Google signs the JWT audience with the exact string you paste in the Chat API Configuration tab.
- If your logs show a `service-NNN@gcp-sa-gsuiteaddons.iam.gserviceaccount.com` identity, add it to `GOOGLE_CHAT_IDENTITIES`.
- Full details: [Google Chat troubleshooting](integrations/google-chat.md#troubleshooting).

### `401 Token has wrong audience` (Google Chat)
For HTTP-endpoint Chat apps, the audience is **always** the endpoint URL, never the project number. See [Google Chat setup step 2](integrations/google-chat.md).

### `403 Forbidden: ACCESS_TOKEN_SCOPE_INSUFFICIENT` (Google Chat async replies)
- The outbound credential is missing the `chat.bot` scope.
- **Fix**: set `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` to a service-account JSON key. **User ADC from `gcloud auth application-default login` cannot supply this scope** — it's restricted to app authentication.
- Full details: [Google Chat authentication](integrations/google-chat.md#authentication-for-async-replies).

### `Error 400: invalid_scope` when running `gcloud auth application-default login`
`chat.bot` is restricted and cannot be granted to user credentials. Don't try to work around this — use a service account key instead. See above.

### "I set `user_role: admin` in the ADK Dev UI but I'm still denied"
You forgot the `_role_set_by_server: true` lock flag. Without it, `ensure_default_role()` resets `user_role` back to `viewer` on every turn. Full walk-through: [Testing RBAC across surfaces](rbac-testing.md#testing-in-adk-web-adk-web).

### "I changed `SLACK_ADMIN_USERS` / `GOOGLE_CHAT_ADMIN_EMAILS` but I'm still viewer"
The role is resolved **once per thread**, at session creation. Start a new thread — the existing one has the old role baked in.

### "Access denied — but I expected a confirmation prompt"
RBAC runs **before** the confirmation gate by design ([ADR-001 § Plugin execution order](adr/001-rbac.md#plugin-execution-order)). Escalate the user's role first.

---

## LLM provider errors

### `google.api_core.exceptions.PermissionDenied`
Vertex AI calls need `gcloud auth application-default login` **and** the project ID set:
```bash
export GOOGLE_CLOUD_PROJECT=your-project
export GOOGLE_GENAI_USE_ENTERPRISE=TRUE
```
If you're using AI Studio instead, set `GOOGLE_GENAI_USE_ENTERPRISE=FALSE` and `GOOGLE_API_KEY=…`.

### `429 Resource exhausted` / quota errors
Hot loop in the remediation subgraph? Check `MAX_REMEDIATION_ITERATIONS` (defaults to 3), enforced by `verify_route` in `agents/orrery-assistant/orrery_assistant/remediation.py`. For Gemini, enable context caching ([`CONTEXT_CACHE_MIN_LENGTH`](config/general.md#context-caching)) — it reduces input tokens per call dramatically for tool-heavy agents.

### LLM costs spike unexpectedly
Check the `orrery_llm_tokens_total` Prometheus counter and the cache hit rate. Common cause: caching is disabled (Gemini-only feature) or `CONTEXT_CACHE_MIN_LENGTH` is set too high to ever trigger. See [Deployment → LLM costs](deployment.md#llm-costs-spike-unexpectedly).

---

## Sessions & storage

### Sessions not persisting across restarts
- `DATABASE_URL` isn't being read. The startup logs should print `Using PostgreSQL session store: postgresql+asyncpg://...[REDACTED]@...`. If they say `Using in-memory session store`, the env var isn't wired (check the Secret is mounted via `envFrom` in K8s).
- In-memory sessions are per-process and lost on restart. Use PostgreSQL for anything with >1 replica (SQLite is not supported).

### `DatabaseUnavailableError: PostgreSQL session store unavailable` at startup
`DATABASE_URL` is set but the database can't be reached or used, so the process **fails fast by design** rather than silently degrading to an in-memory store (which would split sessions across replicas and lose them on restart). Check that the DB host/port is reachable from the pod (NetworkPolicy, wrong namespace, DB not up yet) and that the Postgres driver is installed (`uv sync --extra postgres`). The pod will keep `CrashLoopBackOff`-ing until Postgres is ready — which is the intended behavior. For **local dev only**, set `ORRERY_DB_ALLOW_INMEMORY_FALLBACK=1` to fall back to in-memory instead of crashing.

### `Session not found` (Google Chat)
The bot uses `auto_create_session=True`, so this shouldn't normally fire. If it does, confirm `DATABASE_URL` is valid (or unset, for the in-memory store). Note a misconfigured `DATABASE_URL` now fails fast at startup rather than silently running in-memory — see above.

---

## Deployment

### Pods crash-loop on startup
Usually one of:
- Auth on with no `JWT_SECRET` / `JWT_JWKS_URL` — see [above](#jwt_secret-is-required-when-jwt_algorithmhs256-at-startup).
- `DATABASE_URL` points to a host the pod can't reach. Test from a debug pod: `kubectl run -it --rm psql --image=postgres:16 -- psql $DATABASE_URL`.
- Missing Postgres driver — rebuild the image with `uv sync --extra postgres` (the provided `Dockerfile` does this by default).

A missing `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` does **not** crash the pod or fail readiness: `/readyz` checks only the session database. The process starts, and every chat turn fails on the model call. The console's **Check my environment** button reports it.

### Readiness probe flaps
The startup probe allows up to 60 seconds (12 × 5s). Slow cold starts usually come from LLM warm-up calls or blocking client initialization. Full guidance: [Deployment → Readiness probe flaps](deployment.md#readiness-probe-flaps).

### `make run-dev` fails with "address already in use"
Both `make run-dev` (ADK Dev UI) and `docker compose --profile demo up -d` bind `:8000`. Run one or the other — `docker compose down` clears the demo.

---

## Confirmation flow

### Guarded tool runs without asking for confirmation
- Was the agent built with `create_agent()`? `GuardrailsPlugin` handles RBAC, but confirmation is an **agent-level** `before_tool_callback` that `create_agent()` attaches automatically when a tool is guarded. An agent constructed directly as `LlmAgent(...)` does not get it, and neither does one whose `before_tool_callback` was replaced after construction with an unmarked callback. See [Adding a new agent → Wiring](adding-an-agent.md#wiring).
- Is the tool actually decorated? `@confirm("reason")` and `@destructive("reason")` both attach the metadata the callback reads.

### Confirmation loops / agent keeps asking
A pending confirmation is keyed by the tool and a hash of its arguments, and kept for 300 s. The invocation id only tells a re-call (a later turn) from the first call. If the LLM retries with slightly different arguments, the hash changes and the gate fires again. This is by design — it prevents "yes" being reused across different destructive operations.

---

## Observability

### No data on the Prometheus `/metrics` endpoint
`MetricsPlugin` registers the collector but **does not** bind the HTTP server itself. The HTTP front door, the persistent runner and the Google Chat bot start it when `ENABLE_METRICS_SERVER=true` (port `METRICS_PORT`, default 9100); the Slack bot always does. Check that the variable is set and look for `Could not start the Prometheus metrics server` in the logs (usually the port is taken). For a custom integration, call it yourself — see [Metrics Quick Start](metrics.md#quick-start).

### Circuit breaker always open for a specific tool
Check `orrery_circuit_breaker_state{tool="<name>"} == 1` — the breaker opens after 5 failures in a row (default) and stays open for 60 seconds. If the underlying system is genuinely down, you'll see it flip back to half-open on the next probe.

---

## Still stuck?

- Structured logs: every agent emits JSON to stdout. `docker logs -f orrery-assistant | jq` is the fastest way to see what's happening.
- Audit trail: `AuditPlugin` writes one line per tool call with RBAC decisions, args (redacted), and latency.
- File an issue: [github.com/BAHALLA/orrery/issues](https://github.com/BAHALLA/orrery/issues).
