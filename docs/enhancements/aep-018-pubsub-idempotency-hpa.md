# AEP-018: Pub/Sub Worker Idempotency & Backlog-Based HPA

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P0 |
| **Effort** | Medium (3-5 days) |
| **Impact** | High |
| **Dependencies** | AEP-011 (deployment hardening, completed) |

## Gap Analysis

### Current Implementation

The Google Chat Pub/Sub transport (`agents/google-chat-bot/google_chat_bot/pubsub_worker.py`)
pulls events from a subscription and dispatches them into a shared
`GoogleChatHandler`. The Terraform module at
`deploy/terraform/google-chat-bot/` provisions the subscription with a
retry policy (`minimum_backoff=10s`, `maximum_backoff=600s`) and a DLQ
after `max_delivery_attempts=5`.

Two mechanisms protect the system today:
- **Poison-message handling**: malformed JSON is `ack`-ed and dropped, so
  redelivery cannot loop forever.
- **DLQ**: messages exceeding `max_delivery_attempts` are routed to the
  dead-letter topic.

The worker `Deployment` runs a single replica:
```yaml
pubsubWorker:
  enabled: false
  replicaCount: 1
```

### What's missing

1. **No idempotency guard.** Pub/Sub delivers *at-least-once*. A message
   can be redelivered when:
   - the worker acks after a network blip and Pub/Sub never receives the ack,
   - the pod OOMs mid-callback,
   - `handler_timeout` triggers a `nack` after side effects have landed.

   The handler is wired into `GoogleChatHandler.handle_event`, which can
   invoke `@destructive` tools — `restart_deployment`, `scale_deployment`,
   `rollback_deployment`, Kafka partition increases, Docker
   `docker restart`, Alertmanager silence create. **A redelivered message
   would double-act on these tools.**

2. **Single-replica SPOF during incidents.** When things are on fire,
   Chat traffic spikes (operators asking "what's happening?", triggering
   triage runs). One worker means:
   - Rolling updates block on a single pod and Pub/Sub accumulates backlog.
   - Handler latency compounds: `max_messages=4` × slowest triage
     (~60-120s) can queue up minutes of backlog behind one pod.
   - A single node drain blocks incident response.

3. **No backlog-based scaling signal.** CPU/memory HPAs are useless here
   — the worker is mostly I/O-bound on LLM calls. The meaningful signal
   is `pubsub.googleapis.com/subscription/num_undelivered_messages`, but
   nothing reads it today.

### Gap
- Unbounded blast radius from redelivered destructive tool calls.
- No horizontal scaling during incidents.
- No test covering idempotent delivery semantics.

## Proposed Solution

### Step 1: Event-level idempotency store

Add a module-level `IdempotencyStore` with a pluggable backend:

```python
# agents/google-chat-bot/google_chat_bot/idempotency.py

class IdempotencyStore(Protocol):
    async def claim(self, event_id: str, ttl_seconds: int) -> bool:
        """Return True if this worker is the first to see event_id.

        A False return means the event was already processed (or is in
        flight on another replica) and the caller should ack and drop.
        """
```

Two implementations:
- `InMemoryIdempotencyStore` — single-replica dev default, bounded LRU.
- `RedisIdempotencyStore` — production backend using `SETNX` with TTL
  equal to `message_retention_duration` so duplicate deliveries within
  the retention window are short-circuited across replicas.

Dedup key: Google Chat `eventId` (or, when absent, a stable hash of
`spaceName + threadName + createTime + message.text`).

### Step 2: Wire the store into the Pub/Sub callback

In `make_callback`, before dispatching to the handler:

```python
event_id = _extract_event_id(event)
if not await store.claim(event_id, ttl_seconds=retention_seconds):
    logger.info("Duplicate event_id=%s; acking without re-executing", event_id)
    message.ack()
    return
```

Claim happens *before* invoking the handler. The ack happens after
successful completion (unchanged). If the handler crashes after the
claim, the message is nacked and redelivered — but because the claim is
idempotent, the next worker picks it up and proceeds normally. The
claim's TTL covers the retention window, so true redelivery of the same
original message is blocked.

### Step 3: Config + Helm surface

```python
# google_chat_bot/config.py
google_chat_pubsub_idempotency_backend: str = "memory"  # "memory" | "redis"
google_chat_pubsub_idempotency_redis_url: str | None = None
google_chat_pubsub_idempotency_ttl_seconds: int = 3600
```

Helm:
```yaml
pubsubWorker:
  idempotency:
    backend: memory          # switch to redis for replicaCount > 1
    redisUrl: ""             # e.g. redis://redis.data.svc.cluster.local:6379/3
    ttlSeconds: 3600
```

The Helm chart gains a render-time guard that **fails templating** when
`pubsubWorker.replicaCount > 1` and `idempotency.backend == "memory"`,
so this misconfig is impossible to ship.

### Step 4: Backlog-based HPA

Add a new template `pubsub-worker-hpa.yaml` behind
`pubsubWorker.autoscaling.enabled`:

```yaml
{{- if and .Values.pubsubWorker.enabled .Values.pubsubWorker.autoscaling.enabled -}}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "orrery-assistant.pubsubWorker.fullname" . }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "orrery-assistant.pubsubWorker.fullname" . }}
  minReplicas: {{ .Values.pubsubWorker.autoscaling.minReplicas }}
  maxReplicas: {{ .Values.pubsubWorker.autoscaling.maxReplicas }}
  metrics:
    - type: External
      external:
        metric:
          name: pubsub.googleapis.com|subscription|num_undelivered_messages
          selector:
            matchLabels:
              resource.labels.subscription_id: {{ .Values.config.GOOGLE_CHAT_PUBSUB_SUBSCRIPTION }}
        target:
          type: AverageValue
          averageValue: "{{ .Values.pubsubWorker.autoscaling.targetBacklogPerPod }}"
{{- end }}
```

Requires the GKE custom-metrics adapter (`k8s-stackdriver-adapter`).
Document in `docs/integrations/google-chat-pubsub.md` as a prerequisite
for multi-replica deployments.

### Step 5: Integration test with the Pub/Sub emulator

Add an integration test suite gated behind a `@pytest.mark.integration`
marker and `PUBSUB_EMULATOR_HOST`:

1. Start the emulator in `docker-compose.yml` under a new profile.
2. Publish the same message twice.
3. Assert the handler is invoked exactly once.
4. Publish two distinct messages concurrently to a two-replica test
   harness; assert each is handled exactly once (no duplicate,
   no missed).

## Affected Files

| File | Change |
|------|--------|
| `agents/google-chat-bot/google_chat_bot/idempotency.py` | New: `IdempotencyStore` protocol, `InMemoryIdempotencyStore`, `RedisIdempotencyStore`. |
| `agents/google-chat-bot/google_chat_bot/pubsub_worker.py` | Call `store.claim()` in `make_callback`; inject store in `run()`. |
| `agents/google-chat-bot/google_chat_bot/config.py` | Three new settings (`idempotency_backend`, `redis_url`, `ttl_seconds`). |
| `agents/google-chat-bot/pyproject.toml` | `redis>=5.0.0` under an optional `[redis]` extra. |
| `agents/google-chat-bot/tests/test_idempotency.py` | New: 6 unit tests covering claim semantics + TTL expiry. |
| `agents/google-chat-bot/tests/test_pubsub_worker.py` | New: duplicate-delivery and cross-replica tests. |
| `agents/google-chat-bot/tests/test_integration_pubsub.py` | New: emulator-backed integration test (`@pytest.mark.integration`). |
| `deploy/helm/orrery-assistant/values.yaml` | `pubsubWorker.idempotency.*` and `pubsubWorker.autoscaling.*`. |
| `deploy/helm/orrery-assistant/templates/pubsub-worker-deployment.yaml` | Pass idempotency env vars. |
| `deploy/helm/orrery-assistant/templates/pubsub-worker-hpa.yaml` | New: backlog-based HPA. |
| `deploy/helm/orrery-assistant/templates/NOTES.txt` | Warn when `replicaCount > 1` and `backend == memory`. |
| `docs/integrations/google-chat-pubsub.md` | New sections: *Idempotency*, *Scaling on backlog*, custom-metrics prerequisite. |
| `docker-compose.yml` | New `pubsub-emulator` profile for local integration tests. |

## Acceptance Criteria

- [ ] `IdempotencyStore` protocol + two implementations land with ≥6 unit tests.
- [ ] Duplicate delivery of the same `eventId` within the TTL window is
      observed (via log) but does **not** re-invoke `handle_event`.
- [ ] Helm template fails rendering when `replicaCount > 1` and
      `idempotency.backend == "memory"`.
- [ ] Redis-backed store passes a multi-worker integration test where
      two replicas compete for the same message.
- [ ] `pubsub-worker-hpa.yaml` renders valid HPAv2 YAML; manual test on
      GKE with custom-metrics adapter scales 1 → N when backlog exceeds
      `targetBacklogPerPod`.
- [ ] Documentation updated with backend selection guide and
      custom-metrics adapter install pointer.
- [ ] CHANGELOG entry under the next release heading.

## Notes

- **Why Redis, not Postgres?** The project already uses Postgres for
  session state, but idempotency is an extremely hot, short-lived,
  write-heavy path (one SETNX per Chat event). Redis is a better fit
  and doesn't contend with session DB writes. If operators prefer not
  to run Redis, a `PostgresIdempotencyStore` is a trivial follow-up
  using `INSERT ... ON CONFLICT DO NOTHING` with a TTL-cleanup job.

- **Why not rely on Pub/Sub's exactly-once delivery?** Pub/Sub offers
  [exactly-once delivery](https://cloud.google.com/pubsub/docs/exactly-once-delivery)
  for pull subscriptions, but:
  1. It only guarantees no redelivery *within* the ack window —
     cross-worker races outside the window are still possible.
  2. Enabling it requires a subscription flag change and measurable
     latency tradeoff.
  3. Application-level idempotency is defense-in-depth and cheap.
  Consider enabling `enable_exactly_once_delivery = true` in the
  Terraform module as a complementary hardening step.

- **TTL tuning.** `ttl_seconds` should match
  `message_retention_duration` (default `3600s`). Setting it lower opens
  a window for redelivery to slip past the dedup guard; setting it
  higher wastes Redis memory. The Helm chart passes both from one
  variable to keep them aligned.

- **HPA caveat.** The custom-metrics adapter has a ~60s polling lag, so
  backlog-based scaling is not instantaneous. Keep `minReplicas >= 2`
  during known incident windows (use a CronJob to bump minReplicas on
  a schedule) rather than relying purely on reactive autoscaling.

- **Follow-up AEP.** Once idempotency lands, we can safely enable
  at-least-once semantics on other integrations (Slack retry, future
  A2A protocol receivers) by reusing the same store.
