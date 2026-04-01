# AEP-001: ADK-Native Tool Confirmation

| Field | Value |
|-------|-------|
| **Status** | completed |
| **Priority** | P0 |
| **Effort** | Medium (2-3 days) |
| **Impact** | High |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
The project implements a custom confirmation system in `core/ai_agents_core/guardrails.py`:
- `@confirm(reason)` and `@destructive(reason)` decorators attach metadata to tools
- `GuardrailsPlugin` reads this metadata at runtime via `before_tool_callback`
- Confirmations use an args-hash + TTL mechanism to prevent bypass
- The Slack bot (`agents/slack-bot/confirmation.py`) has a separate `ConfirmationStore` with interactive buttons

### What ADK Provides (since v1.14.0)
ADK has a **native Tool Confirmation** system (`FunctionTool` with `require_confirmation`):
- **Boolean confirmation**: `FunctionTool(my_func, require_confirmation=True)` pauses tool execution and requests yes/no from the user
- **Dynamic confirmation**: A function can decide at runtime whether confirmation is needed (e.g., `require_confirmation=lambda amount, ctx: amount > 1000`)
- **Advanced confirmation**: `tool_context.request_confirmation(hint=..., payload=...)` for structured data (not just yes/no)
- **Remote confirmation via REST**: The ADK API server accepts confirmation responses at `/run` or `/run_sse`, enabling webhooks, Slack bots, or email-based approvals
- **Built-in UI support**: The ADK web interface renders confirmation dialogs automatically

### Gap
The project's custom confirmation system duplicates what ADK provides natively, but misses:
1. **Structured payloads** — current system only supports yes/no, not "approve with modifications"
2. **Remote confirmation API** — Slack bot has its own mechanism instead of using ADK's REST API
3. **Resume support** — ADK's confirmation integrates with session resume; the custom system doesn't
4. **UI rendering** — ADK web UI shows confirmation dialogs; custom metadata doesn't

## Proposed Solution

### Step 1: Migrate Destructive Tools to ADK FunctionTool Confirmation
Replace `@destructive` and `@confirm` decorators with ADK-native `FunctionTool(fn, require_confirmation=...)`:

```python
# Before (current)
@destructive("This will permanently delete the topic")
async def delete_kafka_topic(topic_name: str, tool_context: ToolContext) -> dict:
    ...

# After (ADK-native)
async def delete_kafka_topic(topic_name: str, tool_context: ToolContext) -> dict:
    ...

# In agent definition:
tools=[FunctionTool(delete_kafka_topic, require_confirmation=True)]
```

### Step 2: Use Dynamic Confirmation for Conditional Guards
For tools that only need confirmation in certain cases (current `@confirm`):

```python
async def should_confirm_scale(replicas: int, tool_context: ToolContext) -> bool:
    """Only confirm if scaling beyond 10 replicas."""
    return replicas > 10

tools=[FunctionTool(scale_deployment, require_confirmation=should_confirm_scale)]
```

### Step 3: Upgrade Slack Bot to Use ADK Remote Confirmation
Replace `ConfirmationStore` with ADK's REST-based confirmation:
- When a confirmation is needed, ADK emits an `adk_request_confirmation` function call event
- The Slack bot intercepts this event and renders Approve/Deny buttons
- On user action, the bot sends a `FunctionResponse` to the ADK `/run_sse` endpoint

### Step 4: Keep RBAC Separate
RBAC authorization (`authorize()` in `rbac.py`) is a separate concern from confirmation.
Keep it in `GuardrailsPlugin.before_tool_callback` — it should block unauthorized users
*before* the confirmation prompt even appears.

## Affected Files

| File | Change |
|------|--------|
| `core/ai_agents_core/guardrails.py` | Deprecate `@confirm`, `@destructive` decorators; keep metadata for RBAC role inference |
| `core/ai_agents_core/plugins.py` | Remove confirmation logic from `GuardrailsPlugin`; keep RBAC check |
| `agents/kafka-health/kafka_health_agent/tools.py` | Remove decorators from `create_kafka_topic`, `delete_kafka_topic` |
| `agents/kafka-health/kafka_health_agent/agent.py` | Wrap guarded tools in `FunctionTool(..., require_confirmation=...)` |
| `agents/k8s-health/k8s_health_agent/tools.py` | Remove decorators from `scale_deployment`, `restart_deployment` |
| `agents/k8s-health/k8s_health_agent/agent.py` | Wrap guarded tools in `FunctionTool(...)` |
| `agents/slack-bot/slack_bot/confirmation.py` | Rewrite to use ADK remote confirmation API |
| `agents/slack-bot/slack_bot/handler.py` | Update to route `adk_request_confirmation` events |
| `core/tests/test_guardrails.py` | Update tests for new confirmation flow |

## Acceptance Criteria

- [ ] All destructive/confirm tools use ADK `FunctionTool(require_confirmation=...)`
- [ ] ADK web UI shows native confirmation dialogs for guarded tools
- [ ] Slack bot handles confirmations via ADK REST API (`/run_sse`)
- [ ] RBAC still blocks unauthorized users before confirmation prompt
- [ ] Custom `@destructive`/`@confirm` decorators deprecated with clear migration path
- [ ] All existing confirmation tests pass or are updated
- [ ] No regression in args-hash bypass prevention (now handled by ADK natively)

## Notes

- ADK's Tool Confirmation has a known limitation: it does not support `DatabaseSessionService`. Since the project uses SQLite via `DatabaseSessionService`, verify compatibility first. If incompatible, keep the custom system until ADK adds support.
- The `@destructive` and `@confirm` decorators are also used for RBAC role inference. Consider keeping the metadata (or replacing with a simpler `role_required` annotation) even after migrating confirmation logic.
