# ADR-001: Role-Based Access Control for Agent Tools

**Status:** Accepted
**Date:** 2026-03-17
**Author:** Taoufiq

## Context

The platform's agents expose tools that range from read-only queries (`list_topics`, `get_nodes`) to irreversible operations (`delete_kafka_topic`, `restart_pod`). The existing guardrail system (`@destructive`, `@confirm`) gates *execution* but does not gate *who* can execute. Any user interacting with the agent — via the ADK web UI, CLI, or Slack — can trigger any tool, including destructive ones (after confirmation).

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

3. **`authorize()` via `GuardrailsPlugin`** — RBAC is enforced by the `GuardrailsPlugin`, registered once on the `Runner` via `default_plugins()`. The plugin applies globally to every agent and tool:
   ```python
   from orrery_core import default_plugins, RolePolicy
   plugins = default_plugins(role_policy=RolePolicy(overrides={"sensitive_read": Role.OPERATOR}))
   runner = Runner(agent=root_agent, ..., plugins=plugins)
   ```
   RBAC blocks unauthorized users. Tool confirmation is handled natively by ADK's `FunctionTool(require_confirmation=True)` — see [AEP-001](../enhancements/aep-001-adk-native-confirmation.md).

   **Update (2026-10-23):** confirmation is no longer `FunctionTool(require_confirmation=True)`. It is the `require_confirmation()` agent `before_tool_callback`, which `create_agent()` attaches to every agent holding a guarded tool (see [Guardrails](../guardrails.md#why-confirmation-is-wired-at-the-agent-level-not-the-plugin)). ADK-native `request_confirmation` is used only by `AutonomyPlugin` for destructive tools at autonomy level L4.

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

- `core/orrery_core/security/rbac.py` — `Role` enum, `RolePolicy`, `authorize()`, `@requires_role`, `infer_minimum_role()`, `NamespaceScopeGuard`
- `core/tests/test_rbac.py` — role inference, policy overrides, the authorize callback, default-role enforcement, and namespace scoping

### Plugin-based enforcement (replaces per-agent callbacks)

**Update (2026-03-31):** RBAC is now enforced globally via the `GuardrailsPlugin`, registered once on the `Runner` through `default_plugins()`. This replaces the previous approach of wiring `authorize()` as a `before_tool_callback` on every individual agent.

ADK Plugins apply to every agent, tool, and LLM call managed by the Runner — including sub-agents. This eliminates the need to remember to add `authorize()` to each new agent.

```python
from orrery_core import default_plugins
from google.adk.runners import Runner

runner = Runner(
    agent=root_agent,
    app_name="orrery_assistant",
    session_service=session_service,
    plugins=default_plugins(),  # includes GuardrailsPlugin with RBAC
)
```

**Rule:** When adding a new agent, no RBAC wiring is needed — just mark tools with `@confirm` or `@destructive` and the `GuardrailsPlugin` handles enforcement automatically.

### Plugin execution order

**Update (2026-10-23):** the sequence below reflects the current `default_plugins()` order and the agent-level confirmation gate; the original listed `FunctionTool(require_confirmation=True)` as the last step.

```
GuardrailsPlugin.before_agent_callback  →  ensures default viewer role if not server-set

Before each tool call, in registration order (abridged; the first non-None answer stops the chain):
IdentityStateGuardPlugin   →  snapshots identity/authorization keys (reverts tool writes after the call)
AuditPlugin                →  records the attempt, even if a later gate refuses it
AutonomyPlugin             →  refuses what the process's autonomy level (L2/L3/L4) forbids
GuardrailsPlugin           →  authorize(): role check, then namespace scope
ResiliencePlugin, run budgets  →  circuit breaker, call/delegation/repeat budgets
agent before_tool_callback →  require_confirmation(), attached by create_agent()
```

A viewer requesting `create_kafka_topic` (`@confirm` → requires OPERATOR) gets denied by `authorize()` **before** reaching the confirmation prompt. An operator gets past `authorize()` but is then asked to confirm by `require_confirmation()`.

### Namespace scope: restricting *where*, not just *what*

**Update (2026-07-24):** the role check answers which tool a caller may run. It does not answer where, and on a Kubernetes platform that gap matters: `restart_deployment` is the same `@confirm` tool whether it targets `payments` or `kube-system`, but the blast radius is not remotely the same. An operator who is trusted to bounce an application pod is not thereby trusted to bounce the cluster's DNS.

**Correction (2026-10-23):** `restart_deployment` is `@destructive` (admin), not `@confirm`, so the example above does not apply to it as written. The guard covers `@confirm` tools such as `scale_deployment` the same way.

`NamespaceScopeGuard` adds that second axis, opt-in via `ORRERY_PROTECTED_NAMESPACES` (comma-separated `fnmatch` globs — unset leaves it inert, so existing deployments are unchanged):

```bash
ORRERY_PROTECTED_NAMESPACES=kube-system,kube-public,monitoring*
```

The rules, in order:

1. `admin` is unrestricted — the role that may already delete things platform-wide gains nothing from a namespace fence.
2. **Reads are never scoped.** Diagnosing an incident means looking at infrastructure namespaces; a guard that blocked `describe_pod` in `kube-system` would break triage while preventing nothing.
3. A mutating call (`@confirm`/`@destructive`) by a non-admin is checked against the **effective** namespace: the call's `namespace` argument, or — when omitted — the tool's own signature default, since that is where the call will actually land.
4. **Fail closed** when the namespace cannot be resolved: a tool that declares `namespace` as a required parameter but arrives without one, or with a non-string (a list, say, that no glob could match), is refused rather than guessed at.

The two axes compose and are checked in order — role first, then scope — so a denial always names the reason that applies. This is deliberately *not* modelled as extra roles: adding a "namespace-limited operator" tier would multiply the role table by every scoping dimension, while a guard keeps one question ("may this role act here?") in one place.

## Related how-tos

- **[Guardrails & RBAC](../guardrails.md)** — author-level reference: decorators, tiers, per-tool overrides, dry-run mode.
- **[Testing RBAC across surfaces](../rbac-testing.md)** — drive each role from ADK Web, the CLI, Slack, Google Chat, and raw `Runner` code.
