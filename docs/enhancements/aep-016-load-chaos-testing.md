# AEP-016: Load & Chaos Testing Harness

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P2 |
| **Effort** | Medium (3-4 days) |
| **Impact** | Medium |
| **Dependencies** | AEP-011 (completed), AEP-010 (tracing — optional but useful) |

## Gap Analysis

### Current Implementation

The platform ships 468 unit tests and 22 eval scenarios. That's a strong
foundation but tests two things only:

1. **Correctness in isolation** — does a single tool call return the
   expected shape with a mocked client?
2. **Agent routing** — does the LLM pick the right tool for a prompt?

They don't test anything the system will actually encounter in
production:

- **Concurrent load** — does the ADK runner under 50 simultaneous Slack
  events eventually deadlock on the shared `DatabaseSessionService`?
- **Slow / flaky LLM** — when Gemini takes 60 seconds to respond, do
  circuit breakers open correctly, do readiness probes fail, do
  in-flight requests complete on SIGTERM?
- **Circuit breaker behavior** — the `ResiliencePlugin` is covered by
  unit tests, but there's no evidence it actually protects the platform
  when a real downstream (Kafka, K8s API) times out under load.
- **LoopAgent runaway** — the remediation pipeline has `max_iterations=3`,
  but what happens when the verifier is slow and the loop completes
  just before the session times out?
- **Session store contention** — Postgres is supported (AEP-011) but
  never been exercised with concurrent writes.

### What's available

- **Locust** — Python-native load testing, can exercise the Slack
  webhook (`/slack/events`) and the ADK API directly
- **Toxiproxy** — deterministic chaos injection (latency, bandwidth
  throttling, connection drops) for the LLM and Postgres
- **k6** — alternative to Locust with better CI integration
- **Litmus / Chaos Mesh** — Kubernetes-native chaos for in-cluster tests
- **LiteLLM proxy** — can be configured to return canned errors / delays
  for deterministic LLM chaos

## Proposed Solution

### Step 1: Load test harness with Locust

```
tests/load/
├── README.md
├── conftest.py
├── slack_webhook.py       # Locust file simulating Slack events
├── adk_api.py             # Locust file for direct ADK API hits
└── scenarios/
    ├── triage_burst.py    # 20 concurrent "run incident triage"
    └── remediation_loop.py
```

```python
# tests/load/slack_webhook.py
from locust import HttpUser, task, between
import uuid, json

class SlackUser(HttpUser):
    wait_time = between(2, 8)

    @task(3)
    def ask_health(self):
        self.client.post(
            "/slack/events",
            headers={"X-Slack-Signature": "…", "X-Slack-Request-Timestamp": "…"},
            json=self._event("is kafka healthy?"),
        )

    @task(1)
    def trigger_triage(self):
        self.client.post(
            "/slack/events",
            json=self._event("run incident triage on the api namespace"),
        )

    def _event(self, text: str) -> dict:
        return {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": text,
                "user": "U_LOAD",
                "channel": "C_LOAD",
                "ts": f"{uuid.uuid4()}.000000",
            },
        }
```

### Step 2: Makefile targets

```make
load-test: ## Run smoke load test (10 users, 60s)
	uv run locust -f tests/load/slack_webhook.py \
	  --headless -u 10 -r 2 -t 60s \
	  --host http://localhost:3000

load-test-stress: ## Run sustained load (100 users, 10 min)
	uv run locust -f tests/load/slack_webhook.py \
	  --headless -u 100 -r 5 -t 10m \
	  --host http://localhost:3000 \
	  --html load-report.html
```

### Step 3: Chaos tests with Toxiproxy

Introduce a dev-only `docker-compose.chaos.yml` profile that routes the
LLM and Postgres connections through Toxiproxy:

```yaml
services:
  toxiproxy:
    image: ghcr.io/shopify/toxiproxy:latest
    ports:
      - "8474:8474"
      - "8666:8666"  # → postgres

  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: agents
      POSTGRES_USER: agents
      POSTGRES_PASSWORD: devpass
```

Then in `tests/chaos/test_resilience.py`:

```python
async def test_circuit_breaker_opens_on_llm_timeout(toxiproxy):
    # Inject 30s latency on the LLM endpoint
    toxiproxy.proxy("llm").add_toxic("latency", latency=30000)

    # Fire 5 requests — circuit breaker should open after 3 failures
    for _ in range(5):
        with pytest.raises(CircuitBreakerOpen):
            await run_agent_once("is kafka healthy?")

    # Cleanup — breaker should close after cooldown
    toxiproxy.proxy("llm").remove_toxic("latency")
    await asyncio.sleep(10)
    assert await run_agent_once("is kafka healthy?") is not None
```

### Step 4: LoopAgent runaway test

Contract test that verifies the remediation loop never exceeds its
budget even when the verifier is slow:

```python
async def test_remediation_loop_respects_max_iterations():
    with mock_verifier_delay(seconds=2):
        result = await run_remediation(target="my-deploy")
    assert result.iterations <= 3
    assert result.duration_seconds < 20
```

### Step 5: CI integration

```yaml
# .github/workflows/ci.yml (new job, gated by label)
load-smoke:
  name: Load smoke test
  runs-on: ubuntu-latest
  if: contains(github.event.pull_request.labels.*.name, 'run-load')
  services:
    postgres:
      image: postgres:16
      env:
        POSTGRES_DB: agents
        POSTGRES_USER: agents
        POSTGRES_PASSWORD: test
      ports: ["5432:5432"]
  steps:
    - uses: actions/checkout@v6
    - uses: astral-sh/setup-uv@v5
    - run: uv sync --extra postgres
    - run: uv run python -m agents.slack_bot.app &
    - run: make load-test
    - uses: actions/upload-artifact@v4
      with:
        name: load-report
        path: load-report.html
```

## Affected Files

| File | Change |
|------|--------|
| `tests/load/` | New — Locust scenarios |
| `tests/chaos/` | New — Toxiproxy-based chaos tests |
| `docker-compose.chaos.yml` | New — chaos profile with Toxiproxy + Postgres |
| `Makefile` | Add `load-test`, `load-test-stress`, `chaos-test` targets |
| `.github/workflows/ci.yml` | Add opt-in `load-smoke` job gated by label |
| `pyproject.toml` | Add `locust` and `pytest-toxiproxy` as dev deps |
| `docs/testing.md` | New — load/chaos testing guide |

## Acceptance Criteria

- [ ] Locust scenarios for Slack webhook and ADK API
- [ ] `make load-test` runs a 60-second smoke against a local instance
- [ ] Chaos tests verify circuit breaker opens on LLM timeout and recovers after cooldown
- [ ] LoopAgent max-iterations guardrail verified under slow-verifier conditions
- [ ] CI has an opt-in load smoke test gated by the `run-load` PR label
- [ ] Documented baseline metrics (requests/sec the platform sustains before p99 degrades)
- [ ] Session store contention test with Postgres (50 concurrent writers)

## Notes

- Load tests should NOT hit real LLM providers by default — point them
  at a LiteLLM proxy configured to return canned responses, or a mock
  server. Otherwise CI bills become the load test.
- The Locust scenarios double as an integration smoke test for AEP-013
  once authentication lands — they'll need to mint JWTs to pass auth.
- Chaos tests should be deterministic: Toxiproxy's HTTP API makes this
  straightforward. Avoid time-based flakes.
- Baseline numbers from this AEP feed into the HPA thresholds in the
  Helm chart (AEP-011) — if the platform saturates CPU at 3 rps, the
  `targetCPUUtilizationPercentage: 70` needs adjustment.
