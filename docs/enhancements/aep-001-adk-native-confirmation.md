# AEP-001: Tool Confirmation

| Field | Value |
|-------|-------|
| **Status** | completed |
| **Priority** | P0 |
| **Effort** | Medium (2-3 days) |
| **Impact** | High |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
The project implements a confirmation system in `core/ai_agents_core/guardrails.py`:
- `@confirm(reason)` and `@destructive(reason)` decorators attach metadata to tools
- `require_confirmation()` callback factory gates guarded tools via `before_tool_callback` on each agent
- Confirmations use an args-hash + TTL + invocation-ID mechanism to prevent bypass
- The Slack bot (`agents/slack-bot/confirmation.py`) has a separate `ConfirmationStore` with interactive buttons

### ADK-Native `FunctionTool(require_confirmation=True)` — Not Used

ADK provides a native confirmation system (`FunctionTool` with `require_confirmation`), but it has a critical limitation: **confirmation events do not propagate through `AgentTool`**. When a sub-agent is wrapped as an `AgentTool`, the sub-agent runs in an isolated `Runner` with `InMemorySessionService`. The `AgentTool.run_async` event loop only forwards `state_delta` and `content` — it does not forward `requested_tool_confirmations` to the parent runner/UI.

Additionally, the ADK web dev server creates its own `App`/`Runner` without project plugins, so `GuardrailsPlugin.before_tool_callback` does not run in that context.

### Chosen Approach

Confirmation is handled at the **agent level** via `before_tool_callback=require_confirmation()`. This works in all execution contexts:
- **ADK web UI** — callback is part of the agent definition, no plugin needed
- **CLI runner with plugins** — callback runs alongside plugin RBAC checks
- **AgentTool sub-agents** — callback travels with the agent; state (pending hash + TTL) is copied between parent and child sessions by AgentTool

Same-invocation bypass prevention: the callback tracks `invocation_id` in the pending state. LLM auto-retries within the same invocation are re-blocked; only retries from a different invocation (triggered by actual user confirmation) are allowed.

## Affected Files

| File | Change |
|------|--------|
| `core/ai_agents_core/guardrails.py` | `require_confirmation()` with invocation-ID tracking |
| `core/ai_agents_core/plugins.py` | `GuardrailsPlugin` is RBAC-only; confirmation at agent level |
| `agents/kafka-health/.../agent.py` | `before_tool_callback=require_confirmation()` |
| `agents/k8s-health/.../agent.py` | `before_tool_callback=require_confirmation()` |
| `agents/observability/.../agent.py` | `before_tool_callback=require_confirmation()` |
| `core/tests/test_guardrails.py` | Tests for invocation-ID bypass prevention |

## Notes

- `@destructive` and `@confirm` decorators are still used for RBAC role inference (`rbac.py`) and by the `require_confirmation()` callback to detect guarded tools.
- ADK's `FunctionTool(require_confirmation=True)` may work in future ADK versions that properly propagate confirmation events through `AgentTool`. The current approach can be revisited then.
