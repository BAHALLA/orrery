# AEP-008: Skills-Based Tool Organization

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P2 |
| **Effort** | Medium (2-3 days) |
| **Impact** | Medium |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
Tools are defined as flat lists of async functions in each agent's `tools.py` file.
When the devops-assistant loads all sub-agents, the LLM sees 50+ tools simultaneously.
This can cause:
- Tool selection confusion (the LLM picks the wrong tool)
- Context window bloat (all tool descriptions loaded at once)
- No incremental loading (everything is always available)

### What ADK Provides
ADK has **Skills** (since v1.25.0, experimental):
- Self-contained units of functionality following the [Agent Skill specification](https://agentskills.io)
- Three-level loading: L1 (metadata for discovery), L2 (instructions), L3 (resources)
- Only loaded when triggered, minimizing context window impact
- Organized with `SKILL.md`, `references/`, `assets/`, `scripts/` directories
- Can be defined in files or in code via `models.Skill`

### Gap
The project loads all tools upfront. For a DevOps platform with many capabilities,
this creates unnecessary context pressure and increases the chance of tool misselection.

## Proposed Solution

### Step 1: Organize Tools as Skills

```
agents/devops-assistant/skills/
  kafka_diagnostics/
    SKILL.md           # "Kafka cluster diagnostics and management"
    references/
      troubleshooting.md  # Common Kafka issues and resolutions
    assets/
      consumer_lag_thresholds.json

  k8s_operations/
    SKILL.md           # "Kubernetes cluster operations"
    references/
      pod_debugging.md  # Pod troubleshooting flowchart
      scaling_policies.md

  incident_response/
    SKILL.md           # "Incident triage and response"
    references/
      runbook.md       # Standard incident response procedures
    assets/
      severity_matrix.json
```

### Step 2: Define Skill Metadata

```markdown
<!-- skills/kafka_diagnostics/SKILL.md -->
---
name: kafka-diagnostics
description: >
  Kafka cluster health monitoring, topic management, and consumer group analysis.
  Use when the user asks about Kafka brokers, topics, consumer lag, or cluster health.
---

## Instructions

When diagnosing Kafka issues:
1. Start with cluster health to check broker availability
2. Check consumer group lag for affected groups
3. Review topic metadata for partition/replication issues
4. If needed, reference troubleshooting.md for known issue patterns
```

### Step 3: Load Skills in Agent Definition

```python
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset

kafka_skill = load_skill_from_dir(Path(__file__).parent / "skills" / "kafka_diagnostics")
k8s_skill = load_skill_from_dir(Path(__file__).parent / "skills" / "k8s_operations")

skill_toolset = SkillToolset(skills=[kafka_skill, k8s_skill])

root_agent = create_agent(
    name="devops_assistant",
    tools=[skill_toolset],  # Skills loaded on-demand
)
```

## Affected Files

| File | Change |
|------|--------|
| `agents/devops-assistant/skills/` | New: skill definitions |
| `agents/devops-assistant/devops_assistant/agent.py` | Use `SkillToolset` |
| `docs/adding-an-agent.md` | Update guide with skills pattern |

## Acceptance Criteria

- [ ] At least 3 skills defined (Kafka, K8s, incident response)
- [ ] Skills include reference documentation (troubleshooting guides)
- [ ] Skills loaded on-demand (not all at once)
- [ ] Context window usage reduced compared to flat tool loading
- [ ] Skill metadata enables accurate tool selection by the LLM

## Notes

- Skills is an **experimental** ADK feature. The API may change.
- Skills work best when the agent has many diverse capabilities. For single-purpose agents (like `kafka-health` standalone), flat tools are simpler.
- The reference documents in skills can include runbooks, making the agent more autonomous by having operational knowledge loaded on-demand.
