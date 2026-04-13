# Testing RBAC across surfaces

RBAC is enforced uniformly by the `GuardrailsPlugin`, but **how the user's role gets into session state differs per surface**. This page walks through how to exercise each role (`viewer`, `operator`, `admin`) from every integration — ADK Web, the ADK CLI, the persistent runner, Slack, Google Chat, and a raw Python `Runner`.

For the design rationale, see [ADR-001: RBAC](adr/001-rbac.md). For the API surface (`authorize`, `RolePolicy`, `@requires_role`, `set_user_role`), see the [Core Library RBAC section](core/README.md#role-based-access-control-rbac).

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
| ADK CLI (`adk run <agent>`) | `viewer` | No env-var knob — wrap the agent with `core.runner.run_persistent_cli()` or write a small script |
| Persistent CLI (`make run-devops-persistent`) | `admin` (hard-coded) | Edit `core/ai_agents_core/runner.py:141` to change |
| Slack bot | `viewer` unless mapped | Set `SLACK_ADMIN_USERS` / `SLACK_OPERATOR_USERS`; start a **new thread** |
| Google Chat bot | `viewer` unless mapped | Set `GOOGLE_CHAT_ADMIN_EMAILS` / `GOOGLE_CHAT_OPERATOR_EMAILS`; start a **new thread** |
| Custom `Runner` in Python | Whatever your code sets | Call `set_user_role(initial_state, role)` before `create_session()` |

!!! warning "Roles are baked into the session at creation time"
    Once a session exists with a given `user_role`, changing env vars and sending another message in the **same** session / thread does **not** change the role. Always start a new session / new thread after swapping roles.

## Picking a test tool

Every agent has at least one tool per role tier. Good candidates for quickly exercising the gate:

| Role required | Tool | Agent |
|---------------|------|-------|
| `viewer` | `list_topics` | `kafka-health` |
| `operator` (`@confirm`) | `create_kafka_topic` | `kafka-health` |
| `admin` (`@destructive`) | `delete_kafka_topic` | `kafka-health` |
| `admin` (`@destructive`) | `delete_pod` | `k8s-health` |

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
make run-devops              # opens http://localhost:8000
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
make run-devops-persistent
```

To test a non-admin role here, temporarily edit `core/ai_agents_core/runner.py` — change `set_user_role(initial_state, "admin")` at line 141 to `"operator"` or `"viewer"` and re-run.

**B. Write a 10-line script** using the core helper directly. Save as `scripts/try_role.py`:

```python
import asyncio
from ai_agents_core import set_user_role
from ai_agents_core.runner import run_persistent_cli
from devops_assistant.agent import root_agent

# Patch the initial state by monkey-patching set_user_role's default.
# Simpler: just spin up your own Runner — see the "Custom Runner" section below.

asyncio.run(run_persistent_cli(
    agent=root_agent,
    app_name="devops_assistant",
    user_id="taoufiq@example.com",
))
```

## Testing with the Slack bot

Slack resolves the role from the Slack user ID on the first message in a thread.

1. Find your Slack user ID: profile → ⋮ → *Copy member ID* (looks like `U01ABC123`).
2. In `agents/slack-bot/.env`:
   ```
   SLACK_ADMIN_USERS=U01ABC123
   SLACK_OPERATOR_USERS=U02DEF456
   ```
3. Restart the bot: `make run-slack-bot-socket` (Socket Mode, no public URL needed).
4. Open a **new thread** in a channel the bot is in and @-mention it.
5. To retest with a different role: edit `.env`, restart, start **another new thread**. The existing thread keeps the role it was created with.

See [Slack integration → RBAC](integrations/slack.md#role-based-access-control) for the full mapping table.

## Testing with the Google Chat bot

Google Chat resolves the role from the signed-in user's email claim in the token.

1. In your root `.env`:
   ```
   GOOGLE_CHAT_ADMIN_EMAILS=you@example.com
   GOOGLE_CHAT_OPERATOR_EMAILS=ops@example.com
   ```
2. Restart: `make run-google-chat` (and ensure ngrok / the endpoint URL still matches `GOOGLE_CHAT_AUDIENCE` byte-for-byte).
3. DM the bot (or @-mention it in a space) from an account whose email matches.
4. To retest, change `.env`, restart, and start a **new thread** — same caveat as Slack, the role is locked in for the life of the thread's session.

!!! note "Swapping users is easier than swapping roles"
    If you control multiple Workspace accounts, the fastest way to exercise all three tiers is to list one account per tier in the env vars and @-mention from each one. No restart needed.

## Testing with a custom `Runner` in Python

When building your own integration or writing an integration test, set the role explicitly at session creation:

```python
from ai_agents_core import set_user_role, default_plugins
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from devops_assistant.agent import root_agent

session_service = InMemorySessionService()
initial_state = {}
set_user_role(initial_state, "operator")          # ← this is the trusted entry point

session = await session_service.create_session(
    app_name="devops_assistant",
    user_id="test@example.com",
    state=initial_state,
)

runner = Runner(
    app=App(name="devops_assistant", root_agent=root_agent, plugins=default_plugins()),
    session_service=session_service,
)
```

Subsequent turns on this session will carry `operator` authority. To simulate a privilege escalation attempt, try writing `user_role = "admin"` via a tool's `tool_context.state` — `ensure_default_role()` won't reset it mid-run (the lock flag is already `True`), but the `GuardrailsPlugin` treats `state_delta` writes from tools as untrusted relative to the initial role. That's the integration contract: only the server-side entry point should ever call `set_user_role()`.

## Troubleshooting

### "I set `user_role: admin` in the Dev UI but I'm still denied"

You forgot `_role_set_by_server: true`. Without the lock, `ensure_default_role()` resets it to `viewer` on every agent turn.

### "I changed `SLACK_ADMIN_USERS` but I'm still viewer"

Slack resolves the role **once per thread**, at session creation. Start a new thread — the existing one has the old role baked in.

### "Tool confirmation was expected but I got `access_denied` instead"

RBAC runs before the confirmation gate by design (see [ADR-001 § Plugin execution order](adr/001-rbac.md#plugin-execution-order)). Escalate the user's role and try again.

### "I want to deny a read-only tool too"

Add an explicit override:

```python
from ai_agents_core import RolePolicy, Role, default_plugins

policy = RolePolicy(overrides={"list_sensitive_topics": Role.OPERATOR})
plugins = default_plugins(role_policy=policy)
```

Or annotate the tool directly:

```python
from ai_agents_core import requires_role, Role

@requires_role(Role.OPERATOR)
async def list_sensitive_topics() -> dict: ...
```
