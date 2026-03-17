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

3. **`authorize()` as a `before_tool_callback`** — Follows the same factory pattern as `require_confirmation()` and `dry_run()`. Composes naturally:
   ```python
   before_tool_callback=[authorize(policy), require_confirmation()]
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

### Callback placement: per-agent, not centralized

ADK's `before_tool_callback` does **not** propagate from a parent agent to its sub-agents. Each agent executes its own tools with its own callbacks. This means `authorize()` must be registered on **every agent that has tools**, not just the root orchestrator.

For example, the root `devops_assistant` delegates to `kafka_health_agent`. When `kafka_health_agent` runs `create_kafka_topic`, only its own `before_tool_callback` fires — the root agent's callback is never invoked.

**Current wiring (all tool-bearing agents):**

| Agent | Guarded tools | `before_tool_callback` |
|---|---|---|
| `kafka_health_agent` | `@confirm` (create_topic), `@destructive` (delete_topic) | `[authorize(), require_confirmation()]` |
| `k8s_health_agent` | `@confirm` (scale), `@destructive` (restart) | `[authorize(), require_confirmation()]` |
| `observability_agent` | `@confirm` (create_silence), `@destructive` (delete_silence) | `[authorize(), require_confirmation()]` |
| `ops_journal_agent` | none | `authorize()` |
| `docker_agent` (devops) | none | `[authorize()]` |
| Health checkers (devops) | none (read-only) | `[authorize()]` |
| `journal_writer` (devops) | none | `[authorize()]` |
| `devops_assistant` (root) | none (no direct tools) | `[authorize(), require_confirmation()]` |

**Rule:** When adding a new agent with tools, always include `authorize()` in its `before_tool_callback`. If any of its tools use `@confirm` or `@destructive`, also include `require_confirmation()`. Order matters — `authorize()` first, then `require_confirmation()`.

### Callback execution order

```
authorize()            →  blocks if user role < tool's required role
require_confirmation() →  asks "are you sure?" for @confirm/@destructive tools
```

A viewer requesting `create_kafka_topic` (`@confirm` → requires OPERATOR) gets denied by `authorize()` **before** reaching the confirmation prompt. An operator gets past `authorize()` but is then asked to confirm.
