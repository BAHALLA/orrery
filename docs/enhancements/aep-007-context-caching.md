# AEP-007: Context Caching for LLM Cost Reduction

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P1 |
| **Effort** | Low (1 day) |
| **Impact** | High |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
Every agent request sends the full instruction prompt + conversation history to the LLM.
There is no caching of repeated context. The MetricsPlugin tracks token counts but doesn't
optimize them.

### What ADK Provides
ADK has **Context Caching** (since v1.15.0) for Gemini models:
- `ContextCacheConfig(min_tokens=2048, ttl_seconds=600, cache_intervals=5)`
- Configured at the `App` level, applies to all agents
- Caches system instructions and repeated context between requests
- Significantly reduces token usage and response latency

### Gap
DevOps agents have **long, static system instructions** (agent definitions with detailed
tool descriptions) that are re-sent with every request. For the devops-assistant with 5
sub-agents and 50+ tools, this is significant token overhead.

## Proposed Solution

### Step 1: Add Context Cache Config to App

```python
from google.adk.apps.app import App
from google.adk.agents.context_cache_config import ContextCacheConfig

app = App(
    name="devops-assistant",
    root_agent=root_agent,
    context_cache_config=ContextCacheConfig(
        min_tokens=2048,    # Only cache if context > 2048 tokens
        ttl_seconds=600,    # Cache for 10 minutes
        cache_intervals=10, # Refresh after 10 uses
    ),
)
```

### Step 2: Optimize Agent Instructions for Caching

Place static content (tool descriptions, safety guidelines, RBAC rules) at the beginning
of instructions. Dynamic content (current state, user context) should come last so the
static prefix can be cached.

### Step 3: Track Cache Hit Rate

Add a metric to `MetricsPlugin` for cache hit/miss tracking:

```python
cache_hits = Counter("agent_context_cache_hits_total", "Context cache hits")
cache_misses = Counter("agent_context_cache_misses_total", "Context cache misses")
```

## Affected Files

| File | Change |
|------|--------|
| `core/ai_agents_core/runner.py` | Accept `context_cache_config` in `run_persistent()` |
| `agents/devops-assistant/run_persistent.py` | Configure context caching |
| `core/ai_agents_core/metrics.py` | Add cache hit/miss counters |

## Acceptance Criteria

- [ ] Context caching enabled for devops-assistant
- [ ] Token usage reduced by measurable amount (track before/after)
- [ ] Cache TTL and intervals configurable via environment variables
- [ ] Cache metrics exposed on `/metrics` endpoint
- [ ] No impact on response quality (same outputs with caching enabled)

## Notes

- Context caching is only supported for Gemini 2.0+ models. When using Claude/OpenAI via LiteLLM, this feature is not available. Document this limitation.
- For non-Gemini providers, consider implementing application-level response caching for repeated queries (e.g., "Is Kafka healthy?" asked multiple times in a short window).
- The `min_tokens=2048` threshold prevents caching overhead for simple, short requests.
