# ADR-001: Role-Based Access Control for Agent Tools

**Status:** Accepted
**Date:** 2026-03-17
**Author:** Taoufiq

## Context

The platform's agents expose tools that range from read-only queries (`list_topics`, `get_nodes`) to irreversible operations (`delete_kafka_topic`, `restart_pod`). The existing guardrail system (`@destructive`, `@confirm`, `require_confirmation()`) gates *execution* but does not gate *who* can execute. Any user interacting with the agent — via the ADK web UI, CLI, or Slack — can trigger any tool, including destructive ones (after confirmation).

As we move toward multi-user environments (Slack channels, shared web UI), we need a way to restrict what each user is allowed to do based on their role.

## Decision

Implement a three-role hierarchy that reuses the existing guardrail decorator metadata:

```
VIEWER (0)  →  can call unguarded (read-only) tools
OPERATOR (1) →  can also call @confirm tools (mutating)
ADMIN (2)    →  can also call @destructive tools (irreversible)
```

### Key design choices

1. **Reuse guardrail metadata** — The `@destructive` and `@confirm` decorators already classify tools by risk. RBAC derives minimum roles from these decorators automatically via `infer_minimum_role()`. No need to re-annotate every tool.

2. **Role stored in session state** — The user's role is read from `session.state["user_role"]` (a string: `"viewer"`, `"operator"`, or `"admin"`). This is set by the integration layer (Slack bot, web UI, CLI) at session creation. Default is `"viewer"` (least privilege).

3. **`authorize()` via `GuardrailsPlugin`** — RBAC and confirmation are bundled into the `GuardrailsPlugin`, registered once on the `Runner` via `default_plugins()`. The plugin applies globally to every agent and tool:
   ```python
   from ai_agents_core import default_plugins, RolePolicy
   plugins = default_plugins(role_policy=RolePolicy(overrides={"sensitive_read": Role.OPERATOR}))
   runner = Runner(agent=root_agent, ..., plugins=plugins)
   ```
   RBAC runs first (blocks unauthorized users), then guardrails run (prompts authorized users for confirmation).

4. **`RolePolicy` for overrides** — A policy object allows per-tool overrides when the inferred role isn't appropriate:
   ```python
   policy = RolePolicy(overrides={"sensitive_read": Role.OPERATOR})
   ```

5. **`@requires_role` decorator for explicit annotation** — For tools that don't use `@destructive`/`@confirm` but still need access control:
   ```python
   @requires_role(Role.ADMIN)
   def manage_users() -> dict: ...
   ```

### What this does NOT include (deferred)

- **Authentication** — Verifying *who* the user is. The integration layer (Slack OAuth, web UI auth) handles identity. RBAC only checks the role string already in state.
- **User-to-role mapping store** — No database of users and roles. The Slack bot or web UI sets `user_role` based on its own auth system. A future ADR may add a `RoleStore`.
- **Per-resource permissions** — No "this user can delete topic X but not topic Y". Roles apply uniformly to tool types.
- **Audit trail for denials** — Denials are logged via Python `logging.warning()`. A future iteration could feed these into the audit trail.

## Consequences

### Positive

- **Zero re-annotation** — Existing `@destructive`/`@confirm` tools automatically get the correct role requirements.
- **Composable** — Plugs into the existing callback pipeline alongside guardrails, audit logging, and activity tracking.
- **Least-privilege default** — Users with no role assignment get `VIEWER`, which is read-only.
- **Integration-agnostic** — Any frontend (Slack, web, CLI) can set `user_role` in session state.

### Negative

- **Coarse-grained** — Three roles may not be enough for all scenarios. Mitigated by `RolePolicy` overrides and `@requires_role`.
- **Trust the integration layer** — If the Slack bot or web UI doesn't set `user_role`, all users default to `VIEWER`. If it sets it incorrectly, RBAC is bypassed. This is acceptable because the alternative (embedding auth in the agent framework) would couple concerns.

### Neutral

- **No breaking changes** — `authorize()` is opt-in. Agents without it behave exactly as before.

## Implementation

- `core/ai_agents_core/rbac.py` — `Role` enum, `RolePolicy`, `authorize()`, `@requires_role`, `infer_minimum_role()`
- `core/tests/test_rbac.py` — 25 test cases

### Plugin-based enforcement (replaces per-agent callbacks)

**Update (2026-03-31):** RBAC is now enforced globally via the `GuardrailsPlugin`, registered once on the `Runner` through `default_plugins()`. This replaces the previous approach of wiring `authorize()` as a `before_tool_callback` on every individual agent.

ADK Plugins apply to every agent, tool, and LLM call managed by the Runner — including sub-agents. This eliminates the need to remember to add `authorize()` to each new agent.

```python
from ai_agents_core import default_plugins
from google.adk.runners import Runner

runner = Runner(
    agent=root_agent,
    app_name="devops_assistant",
    session_service=session_service,
    plugins=default_plugins(),  # includes GuardrailsPlugin with RBAC + confirmation
)
```

**Rule:** When adding a new agent, no RBAC wiring is needed — just mark tools with `@confirm` or `@destructive` and the `GuardrailsPlugin` handles enforcement automatically.

### Plugin execution order

```
GuardrailsPlugin.before_agent_callback  →  ensures default viewer role if not server-set
GuardrailsPlugin.before_tool_callback   →  authorize() blocks if user role < tool's required role
                                        →  require_confirmation() asks "are you sure?" for guarded tools
```

A viewer requesting `create_kafka_topic` (`@confirm` → requires OPERATOR) gets denied by `authorize()` **before** reaching the confirmation prompt. An operator gets past `authorize()` but is then asked to confirm.
