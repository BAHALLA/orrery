# AEP-004: LoopAgent for Self-Healing Remediation

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P1 |
| **Effort** | Medium (3-4 days) |
| **Impact** | High |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
The project uses three ADK workflow agent types:
- **SequentialAgent**: For ordered pipelines (e.g., `incident_triage_agent`)
- **ParallelAgent**: For concurrent health checks
- **LLM Agent + AgentTool**: For dynamic routing

However, there is **no iterative workflow**. The incident triage agent runs once:
1. Parallel health checks
2. Summarize findings
3. Write to journal

If a remediation action fails or the system doesn't recover, there is no retry loop.

### What ADK Provides
ADK has **LoopAgent** (since v0.1.0) for iterative workflows:
- Executes sub-agents in a loop until a condition is met or `max_iterations` is reached
- Sub-agents can signal termination via `tool_context.actions.escalate = True`
- Supports the pattern: Act -> Check -> Decide (continue or stop)

### Gap
DevOps automation requires **closed-loop remediation**:
- Restart a pod -> check if it's healthy -> if not, try scaling -> check again
- Create a topic -> verify it exists -> if not, retry with different config
- Silence an alert -> verify the silence took effect -> if not, escalate

The current system is **open-loop**: it takes actions but never verifies outcomes.

## Proposed Solution

### Step 1: Create a Remediation Loop Pattern

```python
from google.adk.agents import LoopAgent, SequentialAgent, LlmAgent

# Step 1: The actor - takes remediation action
remediation_actor = LlmAgent(
    name="RemediationActor",
    model=resolve_model(),
    instruction="""
    You are a DevOps remediation agent. Based on the diagnosis in {diagnosis},
    take the most appropriate remediation action using your tools.
    Record what you did in your output.
    """,
    tools=[restart_deployment, scale_deployment, ...],
    output_key="remediation_action",
)

# Step 2: The verifier - checks if remediation worked
remediation_verifier = LlmAgent(
    name="RemediationVerifier",
    model=resolve_model(),
    instruction="""
    Verify whether the remediation action "{remediation_action}" was successful.
    Check the current state of the affected system.
    If the issue is resolved, call the exit_loop tool.
    If not, describe what's still wrong so the actor can try a different approach.
    """,
    tools=[get_deployment_status, get_pod_logs, exit_loop],
    output_key="verification_result",
)

# Step 3: Combine into a loop
remediation_loop = LoopAgent(
    name="RemediationLoop",
    sub_agents=[remediation_actor, remediation_verifier],
    max_iterations=3,  # Safety limit
)
```

### Step 2: Integrate into Incident Triage Pipeline

Extend the existing `incident_triage_agent`:

```python
incident_triage_agent = SequentialAgent(
    name="incident_triage_agent",
    sub_agents=[
        health_check_agent,       # Parallel health checks (existing)
        triage_summarizer,        # Summarize findings (existing)
        remediation_loop,         # NEW: attempt auto-remediation
        journal_writer,           # Record results (existing)
    ],
)
```

### Step 3: Create Exit Loop Tool

```python
from google.adk.tools.tool_context import ToolContext

async def exit_loop(tool_context: ToolContext) -> dict:
    """Signal that remediation is complete and the loop should stop."""
    tool_context.actions.escalate = True
    tool_context.actions.skip_summarization = True
    return {"status": "remediation_complete"}
```

### Step 4: Add Remediation-Specific Scenarios

| Scenario | Loop Steps | Max Iterations |
|----------|-----------|----------------|
| Pod crash loop | Restart -> Check -> Scale -> Check | 3 |
| High consumer lag | Reset offsets -> Check lag -> Scale consumers | 3 |
| Alertmanager noise | Silence -> Verify silence -> Adjust duration | 2 |
| Deployment rollback | Rollback -> Check health -> Verify pods | 2 |

## Affected Files

| File | Change |
|------|--------|
| `agents/devops-assistant/devops_assistant/agent.py` | Add `remediation_loop` to triage pipeline |
| `agents/devops-assistant/devops_assistant/remediation.py` | New: remediation actor/verifier agents |
| `agents/k8s-health/k8s_health_agent/tools.py` | Add `rollback_deployment` tool |
| `core/ai_agents_core/base.py` | Add `create_loop_agent()` factory function |
| `agents/devops-assistant/tests/test_remediation.py` | New: remediation loop tests |

## Acceptance Criteria

- [ ] `create_loop_agent()` factory function added to core
- [ ] Remediation loop integrated into incident triage pipeline
- [ ] Loop exits on successful remediation (via `exit_loop` tool)
- [ ] Loop respects `max_iterations` safety limit
- [ ] Remediation actions are logged to ops journal
- [ ] At least 2 remediation scenarios tested (pod restart, consumer lag)
- [ ] RBAC enforced: only operator/admin roles can trigger remediation loops

## Notes

- Safety is critical: the remediation loop must respect RBAC and confirmation guards. A viewer should never trigger auto-remediation.
- Consider adding a "dry-run" mode where the loop plans actions but doesn't execute them, useful for review before committing to changes.
- The `max_iterations` limit prevents runaway loops but should also be paired with a timeout to prevent long-running remediations.
