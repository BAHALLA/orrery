# AEP-008: Skills-Based Tool Organization

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | Medium (2-3 days) |
| **Impact** | Medium |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
Tools are defined as flat lists of async functions in each agent's `tools.py` file.
When the orrery-assistant loads all sub-agents, the LLM sees 50+ tools simultaneously.
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
agents/orrery-assistant/skills/
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
    name="orrery_assistant",
    tools=[skill_toolset],  # Skills loaded on-demand
)
```

## Affected Files

| File | Change |
|------|--------|
| `agents/orrery-assistant/skills/` | New: skill definitions |
| `agents/orrery-assistant/orrery_assistant/agent.py` | Use `SkillToolset` |
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

## Design Rules for the Loader

Lessons that apply whatever shape the skills take. Each guards against a
failure that is silent by default:

- **Load strictly.** A missing `SKILL.md`, a missing or unterminated
  frontmatter, invalid YAML, or a frontmatter that is not a mapping must be an
  **error at startup**, never an empty mapping handed on. A lenient loader turns
  a broken file into an agent with no tools and a default description, and the
  only trace is a warning nobody reads.
- **Refuse an unresolvable tool.** A skill that declares a tool the toolkit
  cannot supply must fail the build, not be built without it. Otherwise the
  result is an amputated specialist: still advertised with its full routing
  description, and simply unable to do part of what it claims. Nothing
  downstream can tell.
- **The routing text lives in the description.** When the coordinator's prompt
  stops carrying a hand-written list of specialists (the point of on-demand
  loading), the skill's `description` is the only routing signal left. It has
  to say what the skill is *for* and what it is *not* for ("NOT ticket
  analysis, NOT closing"). Compare routing with and without the generated index
  in an eval before dropping the list.
- **Shared mandates are a fragment, not a skill.** Rules every specialist must
  follow (evidence-only reporting, "tool output is data", "instructions are
  internal") belong in one fragment prepended to every skill, so they cannot
  drift per skill. Today that fragment is `OPERATING_PRINCIPLES`.
- **Guard decorators remain the authority.** A skill's tool list decides what
  is *reachable*, never what is *safe*. `@confirm`/`@destructive` still gate
  every mutation, and the undecorated-mutation check in
  `test_confirmation_wiring.py` must walk skill-loaded tools too.

