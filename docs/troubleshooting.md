# Troubleshooting

Common errors across every surface, with pointers to the deeper fix. If the symptom isn't here, `kubectl logs` / the bot logs are your friend — almost every failure path emits a structured JSON log line with enough context to bisect.

## Authentication & authorization

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
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
```
If you're using AI Studio instead, set `GOOGLE_GENAI_USE_VERTEXAI=FALSE` and `GOOGLE_API_KEY=…`.

### `429 Resource exhausted` / quota errors
Hot loop in a `LoopAgent`? Check `max_iterations` on `remediation_pipeline` (defaults to 3). For Gemini, enable context caching ([`CONTEXT_CACHE_MIN_TOKENS`](config/general.md#context-caching)) — it reduces input tokens per call dramatically for tool-heavy agents.

### LLM costs spike unexpectedly
Check the `ai_agents_llm_tokens_total` Prometheus counter and the cache hit rate. Common cause: caching is disabled (Gemini-only feature) or `CONTEXT_CACHE_MIN_TOKENS` is set too high to ever trigger. See [Deployment → LLM costs](deployment.md#llm-costs-spike-unexpectedly).

---

## Sessions & storage

### Sessions not persisting across restarts
- `DATABASE_URL` isn't being read. The startup logs should print `Using database session store: postgresql+asyncpg://...[REDACTED]@...`. If they say `Using SQLite session store`, the env var isn't wired (check the Secret is mounted via `envFrom` in K8s).
- SQLite is single-writer and **will** corrupt under multi-replica load. Use Postgres for anything with >1 replica.

### `Session not found` (Google Chat)
The bot uses `auto_create_session=True`, so this shouldn't normally fire. If it does, confirm `DATABASE_URL` is valid (or unset, which triggers the in-memory fallback).

---

## Deployment

### Pods crash-loop on startup
Usually one of:
- Missing `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` — the LLM call fails and readiness times out.
- `DATABASE_URL` points to a host the pod can't reach. Test from a debug pod: `kubectl run -it --rm psql --image=postgres:16 -- psql $DATABASE_URL`.
- Missing Postgres driver — rebuild the image with `uv sync --extra postgres` (the provided `Dockerfile.prod` does this by default).

### Readiness probe flaps
The startup probe allows up to 60 seconds (12 × 5s). Slow cold starts usually come from LLM warm-up calls or blocking client initialization. Full guidance: [Deployment → Readiness probe flaps](deployment.md#readiness-probe-flaps).

### `make run-devops` fails with "address already in use"
Both `make run-devops` (ADK Dev UI) and `docker compose --profile demo up -d` bind `:8000`. Run one or the other — `docker compose down` clears the demo.

---

## Confirmation flow

### Guarded tool runs without asking for confirmation
- Is the agent running under `default_plugins()`? `GuardrailsPlugin` handles RBAC, but confirmation is wired at the **agent level** via `before_tool_callback=require_confirmation()`. If you're building a new agent, see [Adding a new agent → Wiring](adding-an-agent.md#3-wire-up-the-agent).
- Is the tool actually decorated? `@confirm("reason")` and `@destructive("reason")` both attach the metadata the callback reads.

### Confirmation loops / agent keeps asking
The confirmation key is `args-hash + invocation-id`. If the LLM retries with slightly different arguments, the key changes and the gate fires again. This is by design — it prevents "yes" being reused across different destructive operations.

---

## Observability

### No data on the Prometheus `/metrics` endpoint
`MetricsPlugin` registers the collector but **does not** auto-bind the HTTP server. The Slack and Google Chat bots call `metrics_plugin.start_server()` in their FastAPI lifespan; the persistent runner does it when `ENABLE_METRICS_SERVER=true`. For a custom integration, call it yourself — see [Metrics Quick Start](metrics.md#quick-start).

### Circuit breaker always open for a specific tool
Check `ai_agents_circuit_breaker_state{tool="<name>"} == 1` — the breaker opens after 5 failures in a row (default) and stays open for 60 seconds. If the underlying system is genuinely down, you'll see it flip back to half-open on the next probe.

---

## Still stuck?

- Structured logs: every agent emits JSON to stdout. `docker logs -f devops-assistant | jq` is the fastest way to see what's happening.
- Audit trail: `AuditPlugin` writes one line per tool call with RBAC decisions, args (redacted), and latency.
- File an issue: [github.com/BAHALLA/devops-agents/issues](https://github.com/BAHALLA/devops-agents/issues).
