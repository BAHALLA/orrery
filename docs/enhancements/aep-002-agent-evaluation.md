# AEP-002: Agent Evaluation Framework

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P0 |
| **Effort** | High (5-7 days) |
| **Impact** | Critical |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
The project has **404+ unit tests** with mocked external dependencies. These tests verify:
- Individual tool functions return correct data
- Input validation rejects bad inputs
- RBAC blocks unauthorized users
- Plugins execute in correct order

However, there is **no evaluation of agent behavior** — whether the agent:
- Chooses the correct tool for a given user query
- Follows the expected trajectory (sequence of tool calls)
- Produces quality natural language responses
- Handles multi-turn conversations correctly
- Delegates to the right sub-agent in the orchestrator

### What ADK Provides
ADK has a comprehensive **evaluation framework** with:

1. **Test files** (`.test.json`): Unit-test-like evaluation of single sessions
   - Expected tool trajectory (ordered list of tool calls with args)
   - Expected final response (reference text)
   - Expected intermediate agent responses (for multi-agent)

2. **Eval sets** (`.evalset.json`): Integration-test-like evaluation of complex multi-turn sessions

3. **Built-in evaluation criteria**:
   - `tool_trajectory_avg_score`: Exact match of tool call sequence (default: 1.0)
   - `response_match_score`: ROUGE-1 similarity (default: 0.8)
   - `final_response_match_v2`: LLM-judged semantic match
   - `rubric_based_final_response_quality_v1`: Custom rubric evaluation
   - `rubric_based_tool_use_quality_v1`: Tool usage quality
   - `hallucinations_v1`: Groundedness check
   - `safety_v1`: Safety/harmlessness

4. **User simulation**: Dynamic user prompts for conversation scenario testing

5. **Three execution modes**: Web UI (`adk web`), pytest, CLI (`adk eval`)

### Gap
The project has **zero agent-level evaluation**. Unit tests verify tool correctness but cannot catch:
- The orchestrator routing "check Kafka lag" to the K8s agent instead of Kafka agent
- The agent calling `delete_kafka_topic` when the user asked to "list" topics
- Hallucinated responses about cluster health
- Regression in multi-agent delegation after prompt changes

## Proposed Solution

### Step 1: Create Test Files for Each Agent

Create `.test.json` files for each agent's core scenarios:

```
agents/
  kafka-health/
    tests/
      test_kafka_tools.py           # existing unit tests
      evals/
        cluster_health.test.json    # "Is my Kafka cluster healthy?"
        consumer_lag.test.json      # "Show consumer lag for group X"
        topic_management.test.json  # "Create topic Y with 3 partitions"
  k8s-health/
    tests/
      evals/
        pod_debugging.test.json     # "Why is pod X crashing?"
        scaling.test.json           # "Scale deployment Y to 5 replicas"
  devops-assistant/
    tests/
      evals/
        routing.test.json           # "Check Kafka lag" -> kafka agent
        incident_triage.test.json   # Full triage workflow
```

### Step 2: Define Evaluation Scenarios

Example test file for Kafka agent:

```json
{
  "eval_set_id": "kafka_health_agent_basic",
  "name": "Kafka Health Agent Basic Scenarios",
  "eval_cases": [
    {
      "eval_id": "check_cluster_health",
      "conversation": [
        {
          "invocation_id": "inv-001",
          "user_content": {
            "parts": [{"text": "Is my Kafka cluster healthy?"}],
            "role": "user"
          },
          "final_response": {
            "parts": [{"text": "Your Kafka cluster has 3 brokers, all online."}],
            "role": "model"
          },
          "intermediate_data": {
            "tool_uses": [
              {"name": "get_kafka_cluster_health", "args": {}}
            ],
            "intermediate_responses": []
          }
        }
      ],
      "session_input": {
        "app_name": "kafka_health_agent",
        "user_id": "test_user",
        "state": {"user_role": "viewer"}
      }
    }
  ]
}
```

### Step 3: Add Evaluation Criteria Config

```json
{
  "criteria": {
    "tool_trajectory_avg_score": 1.0,
    "response_match_score": 0.6,
    "safety_v1": 0.9
  }
}
```

Use strict trajectory matching (1.0) since DevOps tools must be called correctly.
Use relaxed response matching (0.6) since phrasing can vary.

### Step 4: Add Orchestrator Routing Evaluation

Critical for the devops-assistant: verify the root agent delegates to the correct specialist:

```json
{
  "eval_id": "route_kafka_query",
  "conversation": [
    {
      "user_content": {"parts": [{"text": "What's the consumer lag for group payment-processors?"}]},
      "intermediate_data": {
        "tool_uses": [
          {"name": "kafka_health_agent", "args": {"request": "..."}}
        ]
      }
    }
  ]
}
```

### Step 5: Integrate with CI/CD

Add evaluation to the GitHub Actions workflow:

```yaml
# .github/workflows/ci.yml
- name: Run agent evaluations
  run: |
    uv run pytest agents/kafka-health/tests/evals/ -v
    uv run pytest agents/k8s-health/tests/evals/ -v
    uv run pytest agents/devops-assistant/tests/evals/ -v
```

### Step 6: Add Hallucination and Safety Checks

For DevOps agents, hallucinations are dangerous (e.g., reporting a healthy cluster when it's down):

```json
{
  "criteria": {
    "hallucinations_v1": 0.95,
    "safety_v1": 0.95
  }
}
```

## Affected Files

| File | Change |
|------|--------|
| `agents/kafka-health/tests/evals/` | New: evaluation test files |
| `agents/k8s-health/tests/evals/` | New: evaluation test files |
| `agents/observability/tests/evals/` | New: evaluation test files |
| `agents/devops-assistant/tests/evals/` | New: routing + triage evaluation |
| `agents/*/tests/conftest.py` | Add eval fixtures |
| `.github/workflows/ci.yml` | Add evaluation step |
| `Makefile` | Add `make eval` target |

## Acceptance Criteria

- [ ] Each agent has at least 5 evaluation scenarios covering core use cases
- [ ] DevOps assistant has routing evaluation (correct agent delegation)
- [ ] Incident triage workflow has end-to-end evaluation
- [ ] Evaluation runs in CI/CD with `make eval`
- [ ] Tool trajectory score >= 1.0 (exact match) for all scenarios
- [ ] Response match score >= 0.6 for all scenarios
- [ ] Hallucination score >= 0.95 for health-check scenarios
- [ ] Evaluation results are visible in CI/CD output

## Notes

- ADK evaluation with Vertex Gen AI Evaluation Service is a paid service. For CI/CD, prefer the `pytest` approach with `tool_trajectory_avg_score` and `response_match_score` (free, fast, deterministic).
- Consider using `rubric_based_tool_use_quality_v1` for the orchestrator to validate "was the right sub-agent chosen?" without relying on exact tool name matching.
- User simulation (`adk eval` with conversation scenarios) is valuable for multi-turn debugging but too slow for CI/CD.
