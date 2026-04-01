# AEP-012: Custom Agent Classes for DevOps Patterns

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P3 |
| **Effort** | Medium (3-5 days) |
| **Impact** | Medium |
| **Dependencies** | AEP-004 (LoopAgent) |

## Gap Analysis

### Current Implementation
All agents use ADK's built-in `LlmAgent`, `SequentialAgent`, and `ParallelAgent` via
factory functions (`create_agent()`, `create_sequential_agent()`, `create_parallel_agent()`).

This works well but limits the ability to implement DevOps-specific patterns that don't
fit neatly into these generic types.

### What ADK Provides
ADK supports **Custom Agents** by subclassing `BaseAgent`:
- Override `_run_async_impl()` for custom execution logic
- Full control over tool selection, state management, and sub-agent coordination
- Can combine LLM reasoning with deterministic logic
- Access to `InvocationContext` for session, state, and memory

### Gap
Several DevOps patterns would benefit from custom agent classes:
1. **Threshold-based escalation**: Auto-escalate to human if severity > threshold
2. **Runbook executor**: Follow a deterministic runbook with LLM-assisted decision points
3. **Canary deployment agent**: Roll out changes incrementally with automated rollback
4. **SLO monitor**: Continuously check SLO budgets and trigger actions on breach

## Proposed Solution

### Step 1: Create a RunbookAgent

A custom agent that follows a structured runbook with LLM-assisted decision points:

```python
from google.adk.agents import BaseAgent

class RunbookAgent(BaseAgent):
    """Executes a structured runbook with LLM-assisted decisions at branch points."""

    def __init__(self, name, runbook_steps, decision_model, **kwargs):
        super().__init__(name=name, **kwargs)
        self.runbook_steps = runbook_steps
        self.decision_model = decision_model

    async def _run_async_impl(self, ctx):
        for step in self.runbook_steps:
            if step.requires_decision:
                # Use LLM to decide which branch to take
                decision = await self._llm_decide(ctx, step)
                step = step.branches[decision]

            # Execute the step's tool
            result = await step.tool(ctx)
            ctx.session.state[f"step_{step.name}"] = result

            if step.is_terminal:
                break

        yield self._create_final_event(ctx)
```

### Step 2: Create an EscalationAgent

```python
class EscalationAgent(BaseAgent):
    """Monitors agent actions and escalates to humans when thresholds are exceeded."""

    def __init__(self, name, inner_agent, escalation_rules, **kwargs):
        super().__init__(name=name, sub_agents=[inner_agent], **kwargs)
        self.escalation_rules = escalation_rules

    async def _run_async_impl(self, ctx):
        async for event in self.sub_agents[0].run_async(ctx):
            severity = self._assess_severity(event)
            if severity > self.escalation_rules.threshold:
                yield self._create_escalation_event(ctx, event, severity)
                return
            yield event
```

### Step 3: Create a CanaryAgent

```python
class CanaryAgent(BaseAgent):
    """Rolls out changes incrementally with automated health checks and rollback."""

    def __init__(self, name, deploy_tool, health_check_tool, rollback_tool,
                 canary_percentages=[10, 25, 50, 100], **kwargs):
        super().__init__(name=name, **kwargs)
        self.canary_percentages = canary_percentages
        self.deploy_tool = deploy_tool
        self.health_check_tool = health_check_tool
        self.rollback_tool = rollback_tool

    async def _run_async_impl(self, ctx):
        for percentage in self.canary_percentages:
            # Deploy to percentage of instances
            await self.deploy_tool(ctx, percentage=percentage)

            # Wait and check health
            await asyncio.sleep(30)
            health = await self.health_check_tool(ctx)

            if not health["healthy"]:
                await self.rollback_tool(ctx)
                yield self._create_rollback_event(ctx, percentage, health)
                return

            yield self._create_progress_event(ctx, percentage)

        yield self._create_success_event(ctx)
```

### Step 4: Add Factory Functions to Core

```python
# core/ai_agents_core/base.py
def create_runbook_agent(name, runbook_steps, **kwargs):
    return RunbookAgent(name=name, runbook_steps=runbook_steps, **kwargs)

def create_escalation_agent(name, inner_agent, threshold, **kwargs):
    return EscalationAgent(name=name, inner_agent=inner_agent,
                           escalation_rules=EscalationRules(threshold=threshold), **kwargs)
```

## Affected Files

| File | Change |
|------|--------|
| `core/ai_agents_core/custom_agents.py` | New: RunbookAgent, EscalationAgent, CanaryAgent |
| `core/ai_agents_core/base.py` | Add factory functions for custom agents |
| `core/ai_agents_core/__init__.py` | Export new agent classes |
| `core/tests/test_custom_agents.py` | New: tests for custom agents |
| `docs/adding-an-agent.md` | Document custom agent patterns |

## Acceptance Criteria

- [ ] `RunbookAgent` follows structured steps with LLM decision points
- [ ] `EscalationAgent` monitors severity and escalates to humans
- [ ] Factory functions available in core for creating custom agents
- [ ] Custom agents integrate with existing plugins (RBAC, metrics, audit)
- [ ] At least one custom agent used in the devops-assistant workflow
- [ ] Tests cover happy path and escalation/rollback scenarios

## Notes

- Custom agents should be used sparingly — most DevOps workflows can be expressed with `SequentialAgent` + `LoopAgent` + `ParallelAgent`. Use custom agents only when the built-in types can't express the pattern.
- The `CanaryAgent` is a stretch goal that requires actual deployment infrastructure to test. Start with `RunbookAgent` as it's immediately useful for incident response.
- Custom agents must yield `Event` objects compatible with ADK's event system for proper plugin integration.
