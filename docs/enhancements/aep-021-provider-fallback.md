# AEP-021: LLM Provider Fallback Chain

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--green">completed</span> |
| **Priority** | <span class="badge badge--amber">P1</span> |
| **Effort** | Low-Medium (2-3 days) |
| **Impact** | High |
| **Dependencies** | none (reuses existing resilience primitives) |

> Pattern borrowed from the Hermes agent architecture (`runtime_provider.py`
> fallback chains). Adapted to Orrery's `resolve_model()` factory and existing
> `CircuitBreaker` / `@with_retry` primitives.

## Implementation (completed)

Shipped in `core/orrery_core/agent/fallback.py`, with two departures from the
proposal below, both made because the proposal's version would have failed in
production:

1. **A retry policy came first.** The proposal assumed per-model retries already
   existed ("keep per-target retries low"). They did not: no agent set
   `http_options.retry_options`, so a single `503` failed the turn.
   `create_agent()` now attaches `HttpRetryOptions` (`ORRERY_LLM_RETRY_ATTEMPTS`,
   default 4 total tries, on 408/429/5xx) to every agent. Gemini reads it
   natively, and ADK's `LiteLlm` maps it to `num_retries`.
2. **Chains are single-family.** The proposal's example chained Claude to Gemini.
   ADK only preserves the ids LiteLLM uses to pair tool calls with their results
   when the *agent's* model is a LiteLLM model. A Gemini-primary agent that fails
   over to Claude mid-conversation therefore sends history the fallback cannot
   pair, and fails with a `400` that does not explain itself. `resolve_model_chain()`
   refuses a mixed chain at startup with that explanation.

Also differs in detail:

- **404 moves the chain on.** A retired preview model answers 404, and that is
  exactly the case a chain should survive. 400/401/403 do not.
- **No mid-answer switch.** Once a model has yielded a response, a later failure
  is re-raised rather than spliced onto a second model's output.
- **Cooldown instead of a breaker.** A model that just failed is tried *last*
  for `MODEL_FALLBACK_COOLDOWN_SECONDS`, but it is never skipped outright. If
  every model is cooling down, all of them are still tried.
- **Pristine request per link.** The request is snapshotted before the first
  attempt, because a provider may mutate it (Gemini appends a user turn).
- `orrery_llm_failover_total{from_model,to_model}`, and a `WARNING` per failover.

Configuration reference: [General configuration → Model-call retry and fallback](../config/general.md#model-call-retry-and-fallback).

## Gap Analysis

### Current Implementation

`resolve_model()` (`core/orrery_core/agent/base.py`) reads `MODEL_PROVIDER` +
`MODEL_NAME` and returns **one** model — a Gemini string, or a single
`LiteLlm(model=...)` for Claude/OpenAI/Ollama. Every agent gets its model through
this one factory via `create_agent()`.

Orrery's resilience layer is real but **scoped to tools**:

- `ResiliencePlugin` + `CircuitBreaker` wrap per-**tool** calls.
- `@with_retry` adds exponential backoff to async **tool** functions.

Nothing protects the **model call itself**. If the configured provider is down,
rate-limited (sustained `429`), or returns `5xx`, the agent turn fails and there
is no second option — a provider incident is a **full platform outage**.

### Why this matters now

The platform is multi-provider *capable* (LiteLLM) but not multi-provider
*resilient*: it can be **configured** for Gemini or Claude, but a running
deployment is pinned to whichever one `MODEL_NAME` names. Given AEP-011's HPA can
scale to several replicas all pointed at the same provider, a provider-side
quota/outage takes down every replica at once. Price and availability also vary
~50x across models, so a fallback chain doubles as a cost-degradation lever
(primary Pro → cheaper Flash under pressure).

### What's available

- LiteLLM already normalizes providers, so a chain is just an **ordered list** of
  `resolve_model()`-style targets.
- `CircuitBreaker` and `@with_retry` already exist — the fallback just needs to
  classify errors (quota / 5xx / timeout = failover; 4xx validation = don't) and
  advance to the next target.
- ADK lets a `BaseLlm` be swapped per request; a thin wrapper `BaseLlm` can front
  the chain transparently to every agent.

## Proposed Solution

A `FallbackLlm` that wraps an ordered list of `BaseLlm` targets and advances on
**retryable** provider errors, reusing the existing circuit breaker per target.

### Step 1: Parse a chain from env

Extend `resolve_model()` to read an optional chain:

```python
# MODEL_FALLBACK_CHAIN="anthropic/claude-sonnet-5,gemini-2.0-flash"
def resolve_model_chain() -> str | BaseLlm:
    primary = resolve_model()  # unchanged default
    raw = os.getenv("MODEL_FALLBACK_CHAIN", "").strip()
    if not raw:
        return primary
    fallbacks = [_resolve_one(spec.strip()) for spec in raw.split(",") if spec.strip()]
    return FallbackLlm([primary, *fallbacks])
```

`create_agent()` calls `resolve_model_chain()` instead of `resolve_model()` — a
one-line change; every agent inherits failover with zero per-agent wiring.

### Step 2: The fallback wrapper

```python
class FallbackLlm(BaseLlm):
    """Tries each backend in order; advances on retryable provider errors."""

    def __init__(self, targets: list[BaseLlm | str]):
        self._targets = [_as_llm(t) for t in targets]
        self._breakers = {i: CircuitBreaker(name=f"llm-{i}") for i in range(len(self._targets))}

    async def generate_content_async(self, llm_request, **kw):
        last_exc = None
        for i, target in enumerate(self._targets):
            if self._breakers[i].is_open():
                continue  # skip a known-bad provider
            try:
                async for resp in target.generate_content_async(llm_request, **kw):
                    yield resp
                self._breakers[i].record_success()
                return
            except Exception as exc:
                if not _is_retryable(exc):  # 4xx/validation: caller's fault, don't failover
                    raise
                self._breakers[i].record_failure()
                logger.warning(
                    "llm_failover", extra={"from_target": i, "error": type(exc).__name__}
                )
                last_exc = exc
        raise LlmChainExhausted("all providers failed") from last_exc
```

### Step 3: Classify errors conservatively

```python
def _is_retryable(exc: Exception) -> bool:
    # Quota / rate-limit / server / timeout → failover.
    # Auth / bad-request / content-filter → do NOT failover (would just fail again).
    code = getattr(exc, "status_code", None)
    if code in (408, 429, 500, 502, 503, 504):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))
```

### Step 4: Observability

- Emit `llm_failover_total{from_provider,to_provider}` and surface open LLM
  breakers alongside the existing tool-breaker state in `MetricsPlugin`.
- Log each failover at WARN with the request id (already a ContextVar) so a
  provider incident is greppable.

## Caveats to document

- **Behavioral drift**: a fallback model may format tool calls or prose
  differently. Keep chains within a capability tier (don't fail Pro → a tiny
  model) and note that evals (AEP-002) run against the *primary* only.
- **Cost inversion**: if the fallback is *pricier*, a primary outage silently
  raises spend — pair with AEP-015 budget alerts.
- **Confirmation state**: failover happens inside one turn, so the
  requester-verified confirmation flow (AEP-013) is unaffected — the pending
  record is keyed by requester, not model.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/agent/base.py` | Add `resolve_model_chain()`; `create_agent()` uses it |
| `core/orrery_core/agent/fallback.py` | New — `FallbackLlm`, `_is_retryable`, `LlmChainExhausted` |
| `core/orrery_core/metrics.py` | Add `llm_failover_total`; expose LLM breaker state |
| `core/tests/test_fallback_llm.py` | New — failover on 429/5xx, no-failover on 4xx, chain-exhausted, breaker skip |
| `docs/config/general.md` | Document `MODEL_FALLBACK_CHAIN` |

## Acceptance Criteria

- [x] `MODEL_FALLBACK_CHAIN` (comma-separated `provider/model` specs) parsed into a `FallbackLlm`
- [x] Empty/unset chain → single-model behavior identical to today (no regression)
- [x] Failover on `408/429/5xx`/timeout; **no** failover on auth/4xx/content-filter
- [x] A recently failed target is tried last (cooldown) — never skipped outright
- [x] `LlmChainExhaustedError` raised (and audited) only when every target fails
- [x] `llm_failover_total` metric + WARN log per failover
- [x] Unit tests cover each error class and the exhausted case

## Notes

- This composes with, not replaces, `@with_retry`: retry handles a **transient
  blip on one provider**; the chain handles a **sustained failure of that
  provider**. Keep per-target retries low (1-2) so the chain advances quickly.
- Start with a two-link chain (primary + one fallback). Longer chains add
  tail-latency risk on a full outage — cap total attempts with a deadline.
