# AEP-015: Cost Observability & Per-Tenant Budgets

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P1 |
| **Effort** | Medium (4-5 days) |
| **Impact** | High |
| **Dependencies** | AEP-010 (tracing) — soft, can ship independently |

## Gap Analysis

### Current Implementation

`MetricsPlugin` in `core/ai_agents_core/plugins.py` already tracks:

- `llm_tokens_total{provider,model,type=input|output}` — cumulative tokens
- Tool call counts and latency histograms
- Circuit breaker state
- Context cache events

This gives a per-model token view but **nothing downstream understands
cost**. There is no:

- Dollar cost rollup per request / session / user / tenant
- Per-tenant budget enforcement (soft or hard)
- Alerting on cost anomalies (a misbehaving LoopAgent could burn
  through thousands of dollars before anyone notices)
- Dashboard showing the cost impact of context caching or model choice
- Chargeback data for multi-tenant deployments

### Why this matters now

AEP-011 landed HPA that can scale to 6 replicas. Combined with the
closed-loop remediation pipeline (AEP-004) and no cost gate, a single
misbehaving agent loop under load is a financial incident waiting to
happen. The existing token metric is necessary but not sufficient —
humans respond to dollars, not tokens, and the price-per-token varies
~50x between Gemini Flash and Claude Opus.

### What's available

- Per-model pricing tables are published by every provider; LiteLLM
  already ships a bundled price list (`litellm.model_cost`).
- Prometheus supports recording rules and alerting on
  `increase(..., [1h])` windows — a cost gauge derived from token
  counters is trivial to build.
- OpenFeature / pydantic-settings handle per-tenant config loading.
- `ActivityPlugin` already records tool calls to session state —
  extending it with a cost field is a small change.

## Proposed Solution

### Step 1: A cost table

Add `core/ai_agents_core/cost.py`:

```python
from __future__ import annotations

# $ per 1K tokens. Keep in sync with provider pricing pages quarterly.
# Source of truth: https://ai.google.dev/pricing, https://www.anthropic.com/pricing,
# https://openai.com/api/pricing. Values are USD.
PRICE_PER_1K_TOKENS: dict[str, dict[str, float]] = {
    "gemini-2.0-flash":     {"input": 0.00010, "output": 0.00040},
    "gemini-2.5-pro":       {"input": 0.00125, "output": 0.01000},
    "claude-3-5-sonnet":    {"input": 0.00300, "output": 0.01500},
    "claude-3-5-haiku":     {"input": 0.00080, "output": 0.00400},
    "claude-sonnet-4-6":    {"input": 0.00300, "output": 0.01500},
    "gpt-4o":               {"input": 0.00250, "output": 0.01000},
    "gpt-4o-mini":          {"input": 0.00015, "output": 0.00060},
}

def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = PRICE_PER_1K_TOKENS.get(model)
    if rates is None:
        return 0.0
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1000
```

Prefer LiteLLM's built-in table when available:

```python
try:
    from litellm import cost_per_token
except ImportError:
    cost_per_token = None
```

### Step 2: Emit a cost metric

Extend `MetricsPlugin.after_model_callback`:

```python
llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "Estimated LLM cost in USD",
    ["provider", "model", "tenant"],
)

async def after_model_callback(self, *, callback_context, llm_response):
    if not (llm_response and llm_response.usage):
        return
    cost = estimate_cost_usd(
        model=llm_response.model,
        input_tokens=llm_response.usage.input_tokens,
        output_tokens=llm_response.usage.output_tokens,
    )
    tenant = callback_context.state.get("tenant", "default")
    llm_cost_usd_total.labels(
        provider=llm_response.provider or "unknown",
        model=llm_response.model,
        tenant=tenant,
    ).inc(cost)
```

### Step 3: Budget enforcement plugin

```python
class BudgetPlugin(BasePlugin):
    """Hard-stop requests once a tenant's rolling budget is exceeded."""

    def __init__(self, *, window_seconds: int = 3600, default_budget_usd: float = 10.0):
        super().__init__(name="budget")
        self._window = window_seconds
        self._default = default_budget_usd
        self._usage: dict[str, deque[tuple[float, float]]] = defaultdict(deque)

    async def on_user_message_callback(self, *, invocation_context, user_message):
        tenant = invocation_context.session.state.get("tenant", "default")
        budget = invocation_context.session.state.get("budget_usd", self._default)
        now = time.time()
        q = self._usage[tenant]
        # Trim old entries
        while q and now - q[0][0] > self._window:
            q.popleft()
        spent = sum(v for _, v in q)
        if spent >= budget:
            logger.warning("tenant_over_budget", extra={"tenant": tenant, "spent_usd": spent})
            return types.Content(
                parts=[types.Part(text=(
                    f"Budget exceeded for tenant '{tenant}': "
                    f"${spent:.2f} / ${budget:.2f} in last {self._window//60}m. "
                    "Contact an admin to raise the limit."
                ))],
                role="model",
            )
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        if not (llm_response and llm_response.usage):
            return
        tenant = callback_context.state.get("tenant", "default")
        cost = estimate_cost_usd(
            llm_response.model,
            llm_response.usage.input_tokens,
            llm_response.usage.output_tokens,
        )
        self._usage[tenant].append((time.time(), cost))
```

### Step 4: Prometheus alert rules

```yaml
# infra/alert_rules.yml additions
groups:
  - name: llm-cost
    rules:
      - alert: LLMSpendSpike
        expr: increase(llm_cost_usd_total[10m]) > 5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "LLM spend > $5 in last 10m (tenant={{ $labels.tenant }})"
          runbook_url: "https://…/runbooks/llm-spend-spike"

      - alert: LLMBudgetExceeded
        expr: sum by (tenant) (increase(llm_cost_usd_total[1h])) > 10
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Tenant {{ $labels.tenant }} exceeded hourly $10 budget"
```

### Step 5: Grafana dashboard

Panels:

- `sum by (model) (increase(llm_tokens_total[1h]))` — token breakdown
- `sum by (tenant) (increase(llm_cost_usd_total[24h]))` — daily cost
- `rate(llm_cost_usd_total[5m])` — real-time spend rate
- `sum(context_cache_hit_total) / sum(context_cache_request_total)` — cache hit rate, correlates with cost savings

Ship a pre-built dashboard JSON under `infra/grafana-dashboards/llm-cost.json`.

## Affected Files

| File | Change |
|------|--------|
| `core/ai_agents_core/cost.py` | New — price table + `estimate_cost_usd()` |
| `core/ai_agents_core/metrics.py` | Add `llm_cost_usd_total` counter |
| `core/ai_agents_core/plugins.py` | Wire `BudgetPlugin` into `default_plugins()` behind env var |
| `core/tests/test_cost.py` | New — unit tests for pricing math |
| `infra/alert_rules.yml` | Add spend spike + budget exceeded alerts |
| `infra/grafana-dashboards/llm-cost.json` | New dashboard |
| `docs/metrics.md` | Document the cost metric and budget env vars |

## Acceptance Criteria

- [ ] `llm_cost_usd_total` Prometheus counter exported with `{provider, model, tenant}` labels
- [ ] Per-tenant rolling budget enforced by `BudgetPlugin` (returns a graceful LLM message, not an exception)
- [ ] `BUDGET_USD_DEFAULT`, `BUDGET_WINDOW_SECONDS` env vars configurable
- [ ] Unit tests cover pricing table lookups, budget windowing, and over-budget rejection
- [ ] Prometheus alert rules for 10-minute spend spike and hourly budget breach
- [ ] Grafana dashboard showing cost-per-model, cost-per-tenant, cache-hit ROI
- [ ] `docs/metrics.md` documents the cost model and how to update the price table

## Notes

- Price tables drift. Add a calendar reminder to re-verify rates quarterly
  and consider importing LiteLLM's `model_cost` dict as a fallback.
- For strict multi-tenant enforcement, tenant identity must come from a
  **verified** auth claim (AEP-013) — without that, a caller can set
  `tenant=other` in session state and bypass their own budget.
- Context caching (AEP-007) already reduces cost on repeated requests —
  the dashboard should show the savings so it's obvious when caching is
  misconfigured or disabled.
