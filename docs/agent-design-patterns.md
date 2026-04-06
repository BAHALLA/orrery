# Agentic Design Patterns Audit

This document analyzes the platform's architecture against the [Google Cloud Agentic Design Patterns](https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system). The platform is classified as a **Hybrid Multi-Agent System** that balances deterministic workflows with dynamic LLM orchestration.

## Summary of Patterns

| Pattern Category | Key Patterns | Implementation in this Project |
| :--- | :--- | :--- |
| **Multi-Agent (MAS)** | Coordinator | `devops_assistant` (Root Agent) uses `AgentTool` to route requests. |
| | Sequential | `incident_triage_agent` runs a fixed pipeline: Triage → Summarize → Save. |
| | Parallel | `health_check_agent` runs K8s, Kafka, and Docker checks concurrently. |
| | Hierarchical | Root Orchestrator → Workflow Agents → Specialist Workers. |
| **Iterative & Feedback** | ReAct | Default behavior for all `LlmAgent` instances (Thought/Action/Observation). |
| | Loop / Refinement | **Proposed (AEP-004)**: `LoopAgent` for self-healing remediation. |
| | Generator/Critic | **QA (AEP-002)**: Evaluation framework uses LLM-as-a-judge. |
| **Specialized** | Human-in-the-Loop | `GuardrailsPlugin` gates tools with `@confirm` and `@destructive`. |
| | Custom Logic | Enforced via `SequentialAgent` and `ParallelAgent` factory functions. |

---

## Detailed Analysis

### 1. Multi-Agent Systems (MAS)

The project leverages the **Coordinator Pattern** as its primary entry point. The root `devops_assistant` does not perform technical tasks itself; instead, it analyzes user intent and delegates to specialized agents.

*   **LLM-Driven Routing (AgentTool):** As defined in [ADR-002](adr/002-agent-tool-vs-sub-agents.md), specialists like `kafka_agent` and `k8s_agent` are exposed as tools. The LLM decides when to invoke them based on their descriptions.
*   **Deterministic Workflows (Sub-agents):** For complex, repeatable tasks like incident triage, the system switches to **Sequential** and **Parallel** patterns. The `incident_triage_agent` ensures that all systems are checked simultaneously before a summary is generated, providing a predictable and high-quality output that pure LLM routing might miss.

### 2. Iterative & Feedback Patterns

Every individual agent in the project follows the **ReAct (Reason and Act)** pattern. When an agent is given a task, it iterates through "Thoughts" and "Actions" (tool calls) until it observes enough information to provide a final response.

**Iterative Refinement** is currently a target for the remediation layer ([AEP-004](enhancements/aep-004-loop-agent-remediation.md)). The proposed `LoopAgent` will implement a **closed-loop remediation** pattern:
1.  **Act**: Attempt a fix (e.g., `restart_pod`).
2.  **Check**: Verify system health.
3.  **Decide**: If still failing, retry with a different action (e.g., `scale_deployment`) or exit.

### 3. Specialized Patterns

The platform implements a robust **Human-in-the-Loop** pattern via the `GuardrailsPlugin`. 
*   **Safety Gating:** Tools marked with `@confirm` or `@destructive` decorators are intercepted by the `before_tool_callback`.
*   **Bypass Prevention:** The system uses an arguments-hash and invocation-ID tracking mechanism to ensure that the agent only proceeds if the user has explicitly provided confirmation for the *exact* operation requested.

---

## References

*   [Google Cloud: Choose a design pattern for an agentic AI system](https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system)
*   [ADR-002: AgentTool vs Sub-Agents](adr/002-agent-tool-vs-sub-agents.md)
*   [AEP-004: LoopAgent for Self-Healing](enhancements/aep-004-loop-agent-remediation.md)
