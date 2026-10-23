# Testing RBAC across surfaces

RBAC is enforced uniformly by the `GuardrailsPlugin`, but **how the user's role gets into session state differs per surface**. This page walks through how to exercise each role (`viewer`, `operator`, `admin`) from every integration — ADK Web, the ADK CLI, the persistent runner, Slack, Google Chat, the authenticated HTTP front door, and a raw Python `Runner`.

For the design rationale, see [ADR-001: RBAC](adr/001-rbac.md). For the API surface (`authorize`, `RolePolicy`, `@requires_role`, `set_user_role`), see the [Core Library RBAC section](core/README.md#role-based-access-control-rbac).

!!! info "RBAC needs a trusted identity to be useful"
    Self-declared roles (e.g. setting `user_role` from a state editor) are only safe for local development. Any production deployment should go through a transport that *verifies* identity before calling `set_user_role()`: Slack signing secrets, Google Chat OIDC tokens, or the JWT front door at `orrery_core.serving.server`. See [Security & auth](config/security.md).

## How roles are resolved

| Session state key | Purpose | Set by |
|-------------------|---------|--------|
| `user_role` | The user's role — one of `"viewer"`, `"operator"`, `"admin"` | `set_user_role()` at a trusted entry point |
| `_role_set_by_server` | Lock flag proving `user_role` came from trusted code — prevents privilege escalation from untrusted state writes | `set_user_role()` sets this to `True` |

Before every agent turn, `GuardrailsPlugin.before_agent_callback` runs `ensure_default_role()`. If `_role_set_by_server` is **not** `True`, `user_role` is forced back to `viewer`. That means you cannot just type `user_role = admin` in a state editor and have it stick — you must either call `set_user_role()` from the integration, or set **both** keys together.

## Per-surface cheat sheet

| Surface | Default role | How to change |
|---------|--------------|---------------|
| ADK Web (`adk web`) | `viewer` | Edit session state: set `user_role` **and** `_role_set_by_server: true`, then start a **new session** |
| ADK CLI (`adk run <agent>`) | `viewer` | No env-var knob — run the agent through `orrery_core.serving.runner.run_persistent()` or write a small script |
| Persistent CLI (`make run-cli PERSIST=1`) | `admin` (hard-coded) | Edit the role stamped in `core/orrery_core/serving/runner.py` to change |
| Slack bot | `viewer` unless mapped | Set `SLACK_ADMIN_USERS` / `SLACK_OPERATOR_USERS`; restart the bot |
| Google Chat bot | `viewer` unless mapped | Set `GOOGLE_CHAT_ADMIN_EMAILS` / `GOOGLE_CHAT_OPERATOR_EMAILS`; restart the bot |
| HTTP front door (`orrery_core.serving.server`) | Derived from JWT every request | Mint a JWT with the matching `JWT_ROLE_CLAIM` value (`admin` / `operator` / `viewer` or aliases) |
| Custom `Runner` in Python | Whatever your code sets | Call `set_user_role(initial_state, role)` before `create_session()` |

!!! note "The chat bots stamp the role every turn"
    Slack and Google Chat call `set_user_role()` on every message, so a new role applies from the next message, in the same thread. The role mappings are read from the environment at startup, so changing them needs a bot restart.

## Picking a test tool

Every agent has at least one tool per role tier. Good candidates for quickly exercising the gate:

| Role required | Tool | Agent |
|---------------|------|-------|
| `viewer` | `list_kafka_topics` | `kafka-health` |
| `operator` (`@confirm`) | `create_kafka_topic` | `kafka-health` |
| `admin` (`@destructive`) | `delete_kafka_topic` | `kafka-health` |
| `admin` (`@destructive`) | `rollback_deployment` | `k8s-health` |

A denial response looks like this (from `authorize()`):

```json
{
  "status": "access_denied",
  "message": "Access denied. The tool 'delete_kafka_topic' requires 'admin' role, but the current user has 'viewer' role."
}
```

The LLM will usually relay this verbatim. If you see a confirmation prompt instead, you were authorized and hit the *next* gate — the confirmation layer. RBAC runs **before** confirmation, so "confirm?" means you passed RBAC.

## Testing in ADK Web (`adk web`)

ADK's Dev UI is the easiest way to inspect state and try each role.

```bash
make run-dev              # opens http://localhost:8000
```

1. Open the Dev UI, pick the agent, and start a session.
2. Click the **State** panel (right sidebar).
3. Add two keys to the session state:
   ```json
   {
     "user_role": "operator",
     "_role_set_by_server": true
   }
   ```
   You must set **both** — `_role_set_by_server: true` is the lock flag that stops `ensure_default_role()` from resetting `user_role` back to `viewer` on the next turn.
4. Send a message that triggers a tool at that role (e.g. *"create a kafka topic called test-rbac with 3 partitions"*).
5. To test a different role, **start a new session** from the Dev UI (the "+" button) and set the state again. Editing state mid-session is unreliable because `ensure_default_role()` has already run once with the old value.

!!! tip "Verify the role actually took effect"
    After sending a message, re-open the State panel. `user_role` should still be what you set. If it reverted to `viewer`, you forgot the `_role_set_by_server: true` flag.

## Testing in the ADK CLI (`adk run`)

`adk run <agent>` launches a plain REPL with no integration layer, so no role is ever set — every user defaults to `viewer`. There is no env-var override for `adk run` itself.

Workarounds, ordered by convenience:

**A. Use the persistent runner** (already wires `set_user_role(..., "admin")`):

```bash
make run-cli PERSIST=1
```

To test a non-admin role here, temporarily edit `core/orrery_core/serving/runner.py` — change `set_user_role(admin_delta, "admin")` to `"operator"` or `"viewer"` and re-run.

**B. Write a 10-line script** using the core helper directly. Save as `scripts/try_role.py`:

```python
import asyncio
from orrery_core.serving.runner import run_persistent
from orrery_assistant.agent import root_agent

# run_persistent() stamps "admin". For another role, spin up your own
# Runner instead — see the "Custom Runner" section below.

asyncio.run(
    run_persistent(
        root_agent,
        app_name="orrery_assistant",
        user_id="taoufiq@example.com",
    )
)
```

## Testing with the Slack bot

Slack resolves the role from the Slack user ID on every message.

1. Find your Slack user ID: profile → ⋮ → *Copy member ID* (looks like `U01ABC123`).
2. In `agents/slack-bot/.env`:
   ```
   SLACK_ADMIN_USERS=U01ABC123
   SLACK_OPERATOR_USERS=U02DEF456
   ```
3. Restart the bot: `make run-slack MODE=socket` (Socket Mode, no public URL needed).
4. @-mention the bot in a channel it is in.
5. To retest with a different role: edit `.env` and restart. The next message uses the new role, in a new thread or the existing one.

See [Slack integration → RBAC](integrations/slack.md#role-based-access-control) for the full mapping table.

## Testing with the Google Chat bot

Google Chat resolves the role from the signed-in user's email claim in the token.

1. In your root `.env`:
   ```
   GOOGLE_CHAT_ADMIN_EMAILS=you@example.com
   GOOGLE_CHAT_OPERATOR_EMAILS=ops@example.com
   ```
2. Restart: `make run-chat` (and ensure ngrok / the endpoint URL still matches `GOOGLE_CHAT_AUDIENCE` byte-for-byte).
3. DM the bot (or @-mention it in a space) from an account whose email matches.
4. To retest, change `.env` and restart. As with Slack, the role is stamped on every message, so the existing thread picks it up.

!!! note "Swapping users is easier than swapping roles"
    If you control multiple Workspace accounts, the fastest way to exercise all three tiers is to list one account per tier in the env vars and @-mention from each one. No restart needed.

## Testing with the HTTP front door (`orrery_core.serving.server`)

The JWT front door is the production replacement for `adk web`. The role is **verified per request** from the JWT — there is no env-var knob; role changes apply on the next call.

1. Start the server with `AUTH_ENABLED=true` and either `JWT_SECRET` (HS256) or `JWT_JWKS_URL` (RS256). See [Security & auth](config/security.md) for full setup.
2. Mint a test token. With `make run-api`, `make dev-token ROLE=operator` mints one against the local secret. By hand, for HS256:

    ```bash
    python -c '
    import jwt, time
    print(jwt.encode({
        "sub": "alice@example.com",
        "aud": "orrery",
        "iss": "https://test",
        "exp": int(time.time()) + 3600,
        "roles": ["operator"],
    }, "your-shared-secret", algorithm="HS256"))
    '
    ```

    !!! note "Human clients"
        Neither token carries an `azp` or `client_id` claim. That is fine while `ORRERY_HUMAN_CLIENTS` is unset (every token is treated as a person, with a startup warning). If it is set, such a token is classified as a **service** principal: its role is capped at `viewer`, autonomy is pinned to L2, and it cannot approve actions. Add an `azp` claim naming a listed client to test as a person. See [ADR-006](adr/006-caller-principals.md).

3. Call `/chat`:

    ```bash
    curl http://localhost:8000/chat \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"message": "create a kafka topic called test-rbac with 3 partitions"}'
    ```

4. To test a different role, mint a token with a different `roles` claim and send again. Same `session_id` is fine — `_auth` is re-stamped per request and `AuthPlugin.before_agent_callback` re-applies the role on every turn.

5. RBAC denial returns inside the agent's response (the HTTP layer returns 200; the agent body explains the denial). Authentication failure returns `401` with `WWW-Authenticate: Bearer`.

!!! tip "Role aliases"
    `extract_role()` accepts `admin` / `orrery-admin` / `orrery_admin` for the admin tier, and the same pattern for operator. Auth0 deployments typically use a namespaced custom claim like `https://your-api/roles`; set `JWT_ROLE_CLAIM` accordingly.

## Testing with a custom `Runner` in Python

When building your own integration or writing an integration test, set the role explicitly at session creation:

```python
from orrery_core import set_user_role, default_plugins
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from orrery_assistant.agent import root_agent

session_service = InMemorySessionService()
initial_state = {}
set_user_role(initial_state, "operator")  # ← this is the trusted entry point

session = await session_service.create_session(
    app_name="orrery_assistant",
    user_id="test@example.com",
    state=initial_state,
)

runner = Runner(
    app=App(name="orrery_assistant", root_agent=root_agent, plugins=default_plugins()),
    session_service=session_service,
)
```

Subsequent turns on this session will carry `operator` authority. To simulate a privilege escalation attempt, try writing `user_role = "admin"` via a tool's `tool_context.state`: `IdentityStateGuardPlugin` (registered first by `default_plugins()`) snapshots the identity and authorization keys before every tool call and reverts any change the call made. That's the integration contract: only the server-side entry point should ever call `set_user_role()`.

## Troubleshooting

### "I set `user_role: admin` in the Dev UI but I'm still denied"

You forgot `_role_set_by_server: true`. Without the lock, `ensure_default_role()` resets it to `viewer` on every agent turn.

### "I changed `SLACK_ADMIN_USERS` but I'm still viewer"

The mapping is read from the environment when the bot starts. Restart the bot; the next message (in any thread) uses the new role.

### "Tool confirmation was expected but I got `access_denied` instead"

RBAC runs before the confirmation gate by design (see [ADR-001 § Plugin execution order](adr/001-rbac.md#plugin-execution-order)). Escalate the user's role and try again.

### "I want to deny a read-only tool too"

Add an explicit override:

```python
from orrery_core import RolePolicy, Role, default_plugins

policy = RolePolicy(overrides={"list_sensitive_topics": Role.OPERATOR})
plugins = default_plugins(role_policy=policy)
```

Or annotate the tool directly:

```python
from orrery_core import requires_role, Role


@requires_role(Role.OPERATOR)
async def list_sensitive_topics() -> dict: ...
```
