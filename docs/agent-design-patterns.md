# Agentic Design Patterns Audit

This document analyzes the platform's architecture against the [Google Cloud Agentic Design Patterns](https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system). The platform is classified as a **Hybrid Multi-Agent System** that balances deterministic workflows with dynamic LLM orchestration.

## Summary of Patterns

| Pattern Category | Key Patterns | Implementation in this Project |
| :--- | :--- | :--- |
| **Multi-Agent (MAS)** | Coordinator | `orrery_chat_agent` uses `AgentTool` to route requests. |
| | Workflow Graph | `orrery_triage_workflow` is an ADK 2.0 graph executing a fixed pipeline. |
| | JoinNode Barrier | `health_join` synchronizes the concurrent checkers, one generated per enabled specialist's `HealthCheck`. |
| | Hierarchical | Root Orchestrator → Specialist Workers / triage agent (all `AgentTool`s). |
| **Iterative & Feedback** | ReAct | Default behavior for all `LlmAgent` instances (Thought/Action/Observation). |
| | Graph Cycles | Remediation subgraph uses `verify_route` to loop: act → verify → retry. |
| | Generator/Critic | Evaluation framework uses LLM-as-a-judge (AEP-002). |
| **Specialized** | Human-in-the-Loop | `require_confirmation()` (attached by `create_agent()`) gates tools with `@confirm` and `@destructive`; `GuardrailsPlugin` enforces RBAC first. |
| | Custom Logic | Enforced via native Python `FunctionNode`s like `triage_route`. |

## Composition at a Glance

```mermaid
graph TD
    ROOT[orrery_chat_agent<br/>LLM Orchestrator]

    ROOT -->|AgentTool| KAFKA[kafka_health_agent]
    ROOT -->|AgentTool| K8S[k8s_health_agent]
    ROOT -->|AgentTool| OBS[observability_agent]
    ROOT -->|AgentTool| ES[elasticsearch_agent]
    ROOT -->|AgentTool| DOCKER[docker_agent]
    ROOT -->|AgentTool| JOURNAL[ops_journal_agent]
    ROOT -->|AgentTool| TRIAGE[incident_triage_agent]
    
    WORKFLOW[orrery_triage_workflow<br/>Graph]
    WORKFLOW -->|START| HC[health_checkers<br/>Parallel edges]
    HC --> HC1[kafka_health_checker]
    HC --> HC2[k8s_health_checker]
    HC --> HC3[docker_health_checker]
    HC --> HC4[observability_health_checker]
    HC --> HC5[elasticsearch_health_checker]
    HC1 & HC2 & HC3 & HC4 & HC5 --> HJ[health_join<br/>JoinNode]
    HJ --> SUM[triage_summarizer]
    SUM --> JW[journal_writer]
    JW --> TROUTE{triage_route}

    TROUTE -->|remediate| ACT[remediation_actor]
    ACT --> VER[remediation_verifier]
    VER --> VROUTE{verify_route}
    VROUTE -->|retry| ACT
```

Deterministic workflows (triage, remediation) are modeled as native graph edges in `orrery_triage_workflow` —
their execution order is fixed. Specialists live behind `AgentTool` on the `orrery_chat_agent` so
the conversational LLM picks them based on the user's intent. See
[ADR-003](adr/003-graph-workflow-inversion.md) for the rationale.

---

## Detailed Analysis

### 1. Multi-Agent Systems (MAS)

The project leverages the **Coordinator Pattern** as its primary entry point. The root `orrery_chat_agent` handles user requests and routes to specialists. A separate deterministic pipeline, `orrery_triage_workflow`, handles complex repetitive tasks.

*   **LLM-Driven Routing (AgentTool):** As defined in [ADR-002](adr/002-agent-tool-vs-sub-agents.md) (and preserved in ADR-003), specialists like `kafka_health_agent` and `k8s_health_agent` are exposed as tools on the `orrery_chat_agent`. The LLM decides when to invoke them based on their descriptions.
*   **Deterministic Workflows (Graph Edges):** For complex, repeatable tasks like incident triage, the system uses ADK 2.0 `Workflow` graph edges. The parallel health checkers ensure that all systems are checked simultaneously before the `health_join` node synchronizes execution and proceeds to the summary.

### 2. Iterative & Feedback Patterns

Every individual agent in the project follows the **ReAct (Reason and Act)** pattern. When an agent is given a task, it iterates through "Thoughts" and "Actions" (tool calls) until it observes enough information to provide a final response.

**Iterative Refinement** is implemented in the remediation layer using a graph-based cyclic subgraph:

1.  **Act**: The `remediation_actor` attempts a fix, using the actions the enabled specialists declare in `SpecialistSpec.remediation` (for Kubernetes: restart, scale, or roll back a deployment).
2.  **Verify**: The `remediation_verifier` checks system health using diagnostic tools.
3.  **Decide**: The `verify_route` node (a deterministic `FunctionNode`) increments a state counter. If resolved, it routes to `"done"`. If still failing, it routes back to `"retry"` — up to 3 iterations.

**Planning (opt-in).** Reasoning-heavy agents — the root orchestrator, the `triage_summarizer`, and the `remediation_actor` — accept an optional ADK planner via `create_agent(planner=…)`. Set `ORRERY_PLANNER=plan_react` to attach a provider-agnostic [`PlanReActPlanner`](https://adk.dev/agents/llm-agents/) (works with Gemini, Claude, OpenAI, and Ollama via LiteLLM); set `ORRERY_PLANNER=builtin` to use Gemini's native thinking tokens (`BuiltInPlanner`). The planner is resolved once at import time and shared across the opted-in agents in `orrery-assistant`. Tool-leaf agents — per-system health checkers, the remediation verifier, the journal writer — intentionally skip the planner: they execute one short tool sequence per turn, so an extra reasoning pass would add latency without changing the output. Default is **no planner** (zero behavior change). See [Configuration → LLM Provider](config/general.md#planning) for the env-var matrix.

### 3. Specialized Patterns

The platform implements a robust **Human-in-the-Loop** pattern. `GuardrailsPlugin` enforces RBAC; the confirmation itself is `require_confirmation()`, an agent `before_tool_callback` that `create_agent()` attaches to every agent holding a guarded tool.
*   **Safety Gating:** Tools marked with `@confirm` or `@destructive` decorators are intercepted by that `before_tool_callback`.
*   **Bypass Prevention:** The system uses an arguments-hash and invocation-ID tracking mechanism to ensure that the agent only proceeds if the user has explicitly provided confirmation for the *exact* operation requested.

---

## Best Practices: Sizing Tools & Agents

Agentic systems degrade gracefully in capability but sharply in *tool selection accuracy*
as the tool/agent surface grows. The numbers below are the budgets this project targets,
with the reasoning behind each.

### Tool count per agent

| Budget | Value | Rationale |
| :--- | :--- | :--- |
| Sweet spot | **5–15 tools** | LLM function-calling accuracy stays near ceiling. |
| Soft ceiling | **~20 tools** | Measurable accuracy drop on frontier models (see benchmarks below). |
| Hard ceiling | **~25 tools** | Tool-selection errors dominate; eval scores collapse. |

**Why it matters:**

- **Accuracy degradation.** Public benchmarks — the [Berkeley Function-Calling
  Leaderboard (BFCL)](https://gorilla.cs.berkeley.edu/leaderboard.html),
  [Gorilla](https://gorilla.cs.berkeley.edu/), and
  [ToolBench/τ-bench](https://github.com/sierra-research/tau-bench) — consistently show that
  tool-selection accuracy drops as tool count grows, especially when tool descriptions are
  semantically similar (e.g. `get_pod_logs` vs. `describe_pod` vs. `get_pod_events`).
- **Context tax.** Every tool schema is serialised into the system prompt on every turn.
  30 tools × ~150 tokens ≈ 4.5k tokens before the user has spoken. That also churns the
  prompt-cache key and erases the gains documented in [AEP-007](enhancements/aep-007-context-caching.md).
- **Eval combinatorics.** Each tool *pair* is a potential wrong-tool failure mode. 10 tools → 45
  pairs; 30 tools → 435 pairs. Eval coverage (AEP-002) gets expensive fast.

**Current state in this project:**

| Agent | Tools | Status |
| :--- | :--- | :--- |
| `kafka_health_agent` | 24 | ⚠️ near the hard ceiling |
| `k8s_health_agent` | 26 | ❌ over the ~25 hard ceiling |
| `elasticsearch_agent` | 24 | ⚠️ near the hard ceiling |
| `docker_agent` | 17 | ⚠️ past the sweet spot |
| `observability_agent` | 16 | ⚠️ past the sweet spot |
| `ops_journal_agent` | 10 | ✅ in sweet spot |

Most specialists have grown past these budgets as tools were added. `k8s_health_agent` is over the hard ceiling and is the first candidate for a split (for example, its six operator-aware tools); per-agent evals are the check that the larger surfaces still route correctly.

### Agent count per orchestrator

| Budget | Value | Rationale |
| :--- | :--- | :--- |
| Sweet spot | **3–7 direct children** | Routing is tool-selection in disguise; same limits apply. |
| Hard ceiling | **~10 direct children** | Misrouting becomes the dominant eval failure. |

The `orrery_assistant` root currently exposes **6 specialist AgentTools, the
`incident_triage_agent` AgentTool and `load_memory`**, with no sub-agents — 8 tools, inside
the hard ceiling. Adding another specialist should retire or merge an existing one.

### Hierarchy depth

- **Keep it shallow: 2–3 levels.** Each level = one extra LLM turn = added latency (~1–3 s),
  added token cost (context re-summarisation), and a new routing failure mode.
- Current project depth: 2 (`root → specialist` or `root → incident_triage_agent`, which
  holds the health-check tools directly). Stay within 3.

### When to split an agent

Split when **any** of these holds:

1. Tool count crosses **~15**.
2. Tools span **different privilege tiers** (viewer / operator / admin) — splitting
   mirrors RBAC cleanly (see [ADR-001](adr/001-rbac.md)).
3. Tools touch **different external systems** with distinct failure modes — per-system
   circuit breakers ([resilience](https://github.com/BAHALLA/orrery/blob/main/core/orrery_core/reliability/resilience.py)) work better when
   isolated.
4. The system prompt starts containing *"if the user asks X use tool Y; if Z use tool W"* —
   that routing belongs in sub-agent selection, not prose.
5. A tool group **drags down the eval score** — isolate and iterate.

### When NOT to split

- Three tools always called together don't need their own agent.
- "Separation of concerns": agents are not microservices. Every extra agent adds one LLM
  turn on the critical path.
- A tool invoked once per session — keep it on the parent.

### Signals you've overgrown

- Orchestrator chooses the wrong sub-agent in **>5 %** of eval runs.
- p95 latency per user turn > **10 s** because of multi-level routing.
- Context-cache hit rate trending down (tool/agent churn).
- Prose disambiguation creeping into the system prompt.

---

## Framework & Model Limits

Know where the ceiling is imposed by the model or the framework, not by your design.

### Model-level limits

| Model family | Hard tool-list cap | Practical cap (per public benchmarks / vendor guidance) |
| :--- | :--- | :--- |
| **Gemini 2.x / 2.5** | 128 function declarations per request | Accuracy drops past ~20; keep ≤15 for parallel tool calls. |
| **Claude (Sonnet/Opus 4.x)** | No hard documented cap; limited by context window (200k) | Anthropic's tool-use guide recommends keeping tool count "small"; descriptions matter more than count. |
| **OpenAI (GPT-4.x / o-series)** | 128 tools per request (documented) | Function-calling quality degrades past ~20 similar tools. |
| **Ollama / local models** | Varies; most open models degrade sharply past ~10 tools | Strict budget ≤10; prefer narrow specialists. |

**Implication for multi-provider operation:** because this project supports
`MODEL_PROVIDER` switching via LiteLLM, **design to the tightest cap you might deploy
against** — currently Ollama. A 10-tool budget per agent is a safe floor that works on
every supported provider.

### Framework-level limits (ADK)

- **No hard numeric cap** on tools or sub-agents — ADK delegates these limits to the
  underlying model.
- **Graph cycles** should always enforce an iteration cap. This project uses `MAX_REMEDIATION_ITERATIONS = 3` in `verify_route` (see [ADR-003](adr/003-graph-workflow-inversion.md)). There is no default safety net.
- **Graph tuple fan-out** is bounded only by your Python asyncio thread pool and the
  downstream systems' concurrency limits. Add per-system semaphores where relevant.
- **`session.state`** is a dict shared across agents in a run. It is not size-limited by
  ADK, but large payloads here bloat every subsequent LLM call. Prefer `output_key` with
  small summaries over dumping raw tool output.
- **Plugins apply globally** on the `Runner`. There is no per-agent plugin scoping — keep
  plugins cheap because they run on every tool call.

### Context-window budgeting

Plan the system prompt so that, at steady state:

- Tool schemas **< 10 %** of context window.
- Session state + activity log **< 20 %**.
- Conversation history **< 40 %**.
- Leave **≥ 30 %** headroom for the user turn + tool results + model output.

If any of those is breached, it's a sign to split the agent, summarise state, or enable
context caching ([AEP-007](enhancements/aep-007-context-caching.md)) where the provider
supports it (Gemini today).

---

## When to Reach for A2A (Scaling Across Processes)

The in-process model (`AgentTool`) is the right default. Orrery already **exposes** its
coordinator over A2A ([ADR-007](adr/007-a2a-exposition.md),
[A2A integration](integrations/a2a.md)), so other agents can call it. **Consuming** remote
agents (ADK's `RemoteA2aAgent` as a specialist) is not built. Reach for A2A between
specialists (see [AEP-005](enhancements/aep-005-a2a-protocol.md)) only when one of the
following is true — not to feel more "distributed":

1. **Independent deployment cadence.** A team needs to ship the Kafka agent without
   redeploying the orchestrator (or vice versa).
2. **Independent scaling.** One agent's load profile is radically different from the
   rest — e.g. a log-analysis agent that wants 10× replicas while the orchestrator is
   fine on one.
3. **Cross-language agents.** A Go or Java specialist needs to plug into the Python
   orchestrator.
4. **Cross-org reuse.** Another platform wants to consume one of your agents without
   importing Python code.
5. **Blast-radius isolation.** An agent touches systems that warrant a separate security
   boundary (separate IAM, separate network segment, separate audit stream).
6. **Resource isolation.** An agent has a heavy native dependency (e.g. a vendored
   binary, a large ML model) that shouldn't live inside the orchestrator image.

### When NOT to use A2A

- **To make the system "more microservice-like".** Each hop adds network latency, a new
  failure mode, and an extra serialisation boundary for tool results.
- **For performance.** A2A is strictly slower than in-process calls. It buys deployment
  flexibility, not speed.
- **Before evals exist.** Without the eval harness (AEP-002) you cannot prove the split
  didn't regress behaviour.
- **For one-off fan-out.** If you need parallel work, a graph `Workflow` node-tuple
  edge (fan-out) plus a `JoinNode` barrier already gives you that in-process, without
  a network hop.

### Scaling playbook

| Scale stage | Pattern | Typical trigger |
| :--- | :--- | :--- |
| Single process | `AgentTool` (today) | ≤ a few hundred QPS, one team. |
| Single process, horizontally replicated | Same, behind a load balancer with shared session store | State isolation + stateless agents. |
| A2A per agent | Inbound exposition (shipped, ADR-007) + `RemoteA2aAgent` consumption (not built, AEP-005) | Independent deploy, scale, or ownership. |
| A2A + async bus | A2A + Pub/Sub for long-running work (see google-chat-bot worker) | Work exceeds HTTP timeouts; needs retry/DLQ semantics. |

Each step up adds operational surface area — take it only when the prior step can't
absorb the load or organisational constraint.

---

## References

*   [Google Cloud: Choose a design pattern for an agentic AI system](https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system)
*   [A2A Protocol specification](https://a2a-protocol.org)
*   [Berkeley Function-Calling Leaderboard (BFCL)](https://gorilla.cs.berkeley.edu/leaderboard.html)
*   [Gorilla: Large Language Model Connected with Massive APIs](https://gorilla.cs.berkeley.edu/)
*   [τ-bench (ToolBench)](https://github.com/sierra-research/tau-bench)
*   [ADR-001: RBAC via Guardrail Metadata](adr/001-rbac.md)
*   [ADR-002: AgentTool vs Sub-Agents](adr/002-agent-tool-vs-sub-agents.md) (superseded by ADR-003)
*   [ADR-003: Graph Workflow Inversion](adr/003-graph-workflow-inversion.md)
*   [AEP-002: Agent Evaluation Framework](enhancements/aep-002-agent-evaluation.md)
*   [AEP-004: Closed-loop remediation](enhancements/aep-004-loop-agent-remediation.md)
*   [AEP-005: A2A Protocol Support](enhancements/aep-005-a2a-protocol.md)
*   [AEP-007: Context Caching](enhancements/aep-007-context-caching.md)