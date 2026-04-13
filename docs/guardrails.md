# Guardrails

Every tool on the platform falls into one of three risk tiers, set by decorators on the tool function. The `GuardrailsPlugin` reads that metadata at runtime to enforce **RBAC** (who can call it) and `require_confirmation()` enforces **human-in-the-loop confirmation** (did they confirm this exact call).

Related pages:
- [ADR-001: RBAC](adr/001-rbac.md) — design rationale for the three-role hierarchy.
- [Testing RBAC across surfaces](rbac-testing.md) — how to exercise each tier from ADK Web, Slack, Google Chat, and the CLI.
- [Core Library → RBAC](core/README.md#role-based-access-control-rbac) — `authorize`, `RolePolicy`, `@requires_role`, `set_user_role` reference.

## The three tiers

| Decorator | Risk | Minimum role | Example |
|-----------|------|--------------|---------|
| *(none)* | Read-only | `viewer` | `list_topics`, `get_nodes`, `query_prometheus` |
| `@confirm("reason")` | Mutating but reversible | `operator` | `create_kafka_topic`, `scale_deployment`, `restart_container` |
| `@destructive("reason")` | Irreversible | `admin` | `delete_kafka_topic`, `rollback_deployment`, `remove_image` |

The decorator just attaches metadata — it doesn't change tool behavior at import time. The plugin and callback machinery reads that metadata when the tool is invoked.

## Authoring a tool

```python
from ai_agents_core import confirm, destructive, with_retry
from ai_agents_core.validation import validate_string

@with_retry(max_retries=3)
async def list_topics() -> dict:
    """Read-only — no decorator needed."""
    ...

@confirm("Creates a topic in the cluster.")
async def create_topic(name: str, partitions: int) -> dict:
    """Mutating — operator+ can call, confirmation is required."""
    if err := validate_string(name, "name", max_len=100):
        return err
    ...

@destructive("Permanently deletes all data in the topic.")
async def delete_topic(name: str) -> dict:
    """Irreversible — admin only, confirmation is required."""
    ...
```

The `reason` string surfaces in the confirmation prompt and in the RBAC denial message, so write it in a way that helps the operator decide.

## How enforcement happens

Two independent callbacks fire before every tool call:

```
1. GuardrailsPlugin.before_tool_callback
     └─ authorize()              ← reads user_role from session state
                                    returns "access_denied" if role < required

2. Agent's before_tool_callback
     └─ require_confirmation()   ← reads args-hash + invocation-id
                                    returns "confirmation_required" until the
                                    same (tool, args, invocation) is approved
```

RBAC runs first by design. A viewer asking for `delete_kafka_topic` is denied before they ever see a confirmation prompt — there's no "will you confirm? oh wait, you can't do this anyway" round-trip.

See [ADR-001 § Plugin execution order](adr/001-rbac.md#plugin-execution-order) for the full sequence.

## Why confirmation is wired at the agent level (not the plugin)

`GuardrailsPlugin` in its default `"confirm"` mode only does RBAC — it does **not** attach a confirmation gate. Confirmation is wired per-agent:

```python
from ai_agents_core import create_agent, require_confirmation

root_agent = create_agent(
    name="my_agent",
    ...,
    before_tool_callback=require_confirmation(),
)
```

Rationale: confirmation needs to work identically whether the agent is called as a sub-agent via `AgentTool`, run standalone in `adk web`, or invoked from a custom integration. Attaching it at the agent level guarantees that regardless of how the tool is reached, the same gate fires once.

## Overriding the default role for a specific tool

### Via `RolePolicy`

```python
from ai_agents_core import RolePolicy, Role, default_plugins

policy = RolePolicy(overrides={
    "list_sensitive_topics": Role.OPERATOR,   # read-only, but gated
    "create_kafka_topic": Role.ADMIN,         # elevate from @confirm default
})
plugins = default_plugins(role_policy=policy)
```

### Via `@requires_role`

```python
from ai_agents_core import requires_role, Role

@requires_role(Role.ADMIN)
async def list_audit_log() -> dict:
    """Read-only, but only admins should see it."""
    ...
```

`@requires_role` takes precedence over the decorator-inferred role when both are present.

## Bypassing confirmation in specific contexts

### Slack and Google Chat bots

Both bots replace text-based confirmation with **interactive buttons** (Slack Blocks / Google Chat Card v2). The bot's handler sets `guardrail_mode="none"` on `default_plugins()` to skip the plugin's confirmation gate, then wires a custom `before_tool_callback` that emits a card instead:

- Slack: `agents/slack-bot/slack_bot/confirmation.py`
- Google Chat: `agents/google-chat-bot/google_chat_bot/confirmation.py`

Sub-agents keep their per-agent `require_confirmation()` as a fallback for guarded tools reached without going through the root.

### Dry-run mode

```python
plugins = default_plugins(guardrail_mode="dry_run")
```

Every guarded tool returns a `{"status": "dry_run", "would_execute": ...}` payload without actually running. Useful for demos, CI, or when operators want to preview a plan before enabling real writes.

## What confirmation actually checks

`require_confirmation()` builds a key from `(tool_name, args-hash, invocation-id)` and stores it in session state. The tool runs only when the LLM's follow-up call matches the same key with a confirmation flag set.

Consequences:
- **Arg drift breaks the cache.** If the LLM retries `delete_topic(name="logs")` with `name="logs-v2"`, that's a fresh confirmation.
- **Invocation scope.** The same "yes" cannot silently authorize a different destructive call later in the turn.
- **No state leakage.** Confirmation lives in session state, so restarts invalidate pending approvals.

## Related config

| Variable | Default | Purpose |
|----------|---------|---------|
| `guardrail_mode` (arg to `default_plugins`) | `"confirm"` | `"dry_run"` preview mode, `"none"` for integrations with their own UX |
| `role_policy` (arg to `default_plugins`) | `None` | Per-tool role overrides via `RolePolicy` |
