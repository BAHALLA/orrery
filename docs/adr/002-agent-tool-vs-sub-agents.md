# ADR-002: AgentTool vs Sub-Agents for Multi-Agent Composition

**Status:** Accepted
**Date:** 2026-03-22
**Author:** Taoufiq

## Context

The devops-assistant root orchestrator composes six specialist agents. Two distinct delegation patterns exist in Google ADK:

1. **Sub-agents** (`sub_agents=[]`) — Agents listed as structural children of a parent. Used with `SequentialAgent`, `ParallelAgent`, and `LoopAgent` for deterministic, orchestrator-driven workflows.

2. **AgentTool** (`tools=[AgentTool(agent=...)]`) — An agent wrapped as a callable tool. The parent LLM decides **whether and when** to invoke it, just like any other function call.

Previously, all six agents were in `sub_agents`, mixing two fundamentally different delegation modes:
- `incident_triage_agent` — A deterministic `SequentialAgent` pipeline (always runs health checks → summarize → save). **Orchestrator-driven.**
- `kafka_agent`, `k8s_agent`, `observability_agent`, `docker_agent`, `journal_agent` — The LLM reads their descriptions and picks one based on the user's query. **LLM-driven.**

This violated the ADK best practice: use sub-agents for fixed workflows, use AgentTool for LLM-driven routing.

**Reference:** [Where to use sub-agents versus agents as tools](https://cloud.google.com/blog/topics/developers-practitioners/where-to-use-sub-agents-versus-agents-as-tools) (Google Cloud Blog). These patterns are formally audited in [Agentic Design Patterns](../agent-design-patterns.md).

## Decision

Convert the five LLM-routed specialist agents from `sub_agents` to `AgentTool`. Keep `incident_triage_agent` as the sole sub-agent (deterministic workflow).

### Decision criteria (general rule)

| Question | Sub-agent | AgentTool |
|---|---|---|
| Who decides the execution order? | The orchestrator (fixed pipeline) | The LLM (dynamic routing) |
| Is the workflow deterministic? | Yes — always runs A → B → C | No — depends on user query |
| Do agents need shared state? | Yes — pipeline data flow via `output_key` | No — self-contained request/response |
| Can the agent have multiple parents? | No — ADK enforces single `parent_agent` | Yes — `AgentTool` can wrap any agent |
| Coupling level | Tight — part of the parent's structure | Loose — called like a function |

### When to use sub-agents

- `SequentialAgent` pipelines: fetch → process → save
- `ParallelAgent` fan-out/gather: run N checks concurrently, collect results
- `LoopAgent` iterations: retry/refine until a condition is met
- Any workflow where the execution order is **predetermined**

### When to use AgentTool

- The parent LLM should **choose** which specialist to call based on user intent
- The specialist is a self-contained expert with clear input/output boundaries
- The same agent may be invoked by **multiple** parent agents
- You want **cleaner context** — tool signatures are more structured than sub-agent descriptions for LLM function calling

### What this looks like in code

**Before (all sub-agents):**
```python
root_agent = create_agent(
    name="devops_assistant",
    tools=[],
    sub_agents=[
        incident_triage_agent,  # deterministic workflow
        kafka_agent,            # LLM-routed
        k8s_agent,              # LLM-routed
        observability_agent,    # LLM-routed
        docker_agent,           # LLM-routed
        journal_agent,          # LLM-routed
    ],
)
```

**After (mixed — sub-agents for workflows, AgentTool for specialists):**
```python
from ai_agents_core import AgentTool

root_agent = create_agent(
    name="devops_assistant",
    tools=[
        AgentTool(agent=kafka_agent),
        AgentTool(agent=k8s_agent),
        AgentTool(agent=observability_agent),
        AgentTool(agent=docker_agent),
        AgentTool(agent=journal_agent),
    ],
    sub_agents=[
        incident_triage_agent,  # deterministic SequentialAgent pipeline
    ],
)
```

## Consequences

### Positive

- **Correct delegation semantics** — Deterministic workflows stay as sub-agents; LLM-routed specialists become tools. Each pattern is used for its intended purpose.
- **Better LLM routing** — AgentTool exposes agents as function signatures, which is more natural for LLM function calling than sub-agent description matching.
- **Reusability** — Specialist agents are no longer bound to a single parent. They can be wrapped in `AgentTool` by any future orchestrator without hitting ADK's single-parent constraint.
- **Cleaner separation** — The root agent's `sub_agents` list now only contains structural workflows, making the architecture self-documenting.

### Negative

- **State sharing changes** — AgentTool forwards state/artifact changes back to the parent, but the interaction is more transactional than shared-state sub-agents. For this project, this is fine since the specialist agents don't rely on shared session state with the root.

### Neutral

- **No behavioral change** — The LLM still routes to the same specialists for the same queries. The delegation mechanism changes, but the user experience is identical.
- **Callbacks still per-agent** — As noted in ADR-001, each agent's `before_tool_callback` fires independently. This is unchanged by the AgentTool pattern.

## Implementation

- `core/ai_agents_core/base.py` — Widened `tools` type to `Sequence[Callable[..., Any] | BaseTool]`, re-exported `AgentTool`
- `core/ai_agents_core/__init__.py` — Added `AgentTool` export
- `agents/devops-assistant/devops_assistant/agent.py` — Converted 5 specialist agents from `sub_agents` to `AgentTool`
