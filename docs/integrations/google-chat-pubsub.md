# Google Chat: Pub/Sub Setup

The Pub/Sub transport is ideal for bots living in **private networks** (e.g., private GKE clusters)
where Google Chat cannot reach the bot via HTTP.

## Architecture

Google Chat publishes events to a **Pub/Sub Topic**, and the bot pulls from a **Subscription**.
The bot is outbound-only — no Ingress or public Load Balancer is required.

## 1. GCP Infrastructure (Terraform)

Use the provided module at [`deploy/terraform/google-chat-bot`](https://github.com/BAHALLA/orrery/tree/main/deploy/terraform/google-chat-bot) to provision the required resources:

```hcl
module "orrery_chat_bot" {
  source = "../../deploy/terraform/google-chat-bot"

  project_id          = "your-project-id"
  k8s_namespace       = "orrery"
  k8s_service_account = "orrery-chat-bot"
}
```

This creates:
- The Pub/Sub **Topic** and **Subscription** (with retry policy and optional DLQ).
- A **Google Service Account (GSA)** with subscriber permissions.
- **Workload Identity** bindings for GKE.
- Optional **Vertex AI User** binding for the Gemini provider.

### Module variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `project_id` | — | GCP project hosting the Pub/Sub resources and GSA. |
| `name` | `orrery-chat` | Prefix for the topic / subscription / GSA names. |
| `k8s_namespace` | — | K8s namespace running the worker. |
| `k8s_service_account` | `orrery-chat-bot` | KSA bound to the GSA via Workload Identity. |
| `chat_publisher_email` | `chat-api-push@system.gserviceaccount.com` | SA that Google Chat uses to publish events. **Override** for Workspace Add-ons or when `constraints/iam.allowedPolicyMemberDomains` blocks the default. |
| `enable_vertex_ai` | `true` | Grant `roles/aiplatform.user` to the GSA. Set to `false` if you use OpenAI/Anthropic/Ollama. |
| `vertex_ai_project_id` | `var.project_id` | GCP project where Vertex AI is called (supports cross-project). |
| `enable_dead_letter` | `true` | Create a DLQ topic + diagnostics subscription. |
| `max_delivery_attempts` | `5` | Redelivery attempts before routing to the DLQ. |
| `ack_deadline_seconds` | `60` | Initial ack deadline (auto-extended while a callback runs). |
| `message_retention_duration` | `3600s` | How long unacked messages are kept. |
| `dlq_subscribers` | `[]` | IAM members (e.g. `group:sre-oncall@company.com`) granted `roles/pubsub.subscriber` on the DLQ subscription for triage. |
| `labels` | `{component, managed-by}` | Labels applied to all resources. |

> **Workspace Add-ons / Domain Restricted Sharing**
> If your Chat app is a Workspace Add-on, or your org enforces
> `constraints/iam.allowedPolicyMemberDomains`, the default
> `chat-api-push@system.gserviceaccount.com` will fail to bind. Copy the
> exact SA from the Chat API console → Configuration → Connection
> settings → "Service Account Email" and pass it as
> `chat_publisher_email` (typically
> `service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com`).

## 2. Configure the Chat API

After applying Terraform, you must complete two manual steps in the **Google Chat API** -> **Configuration** tab:

1.  **App Authentication**: Select "Service Account" and paste the `gsa_email` from Terraform output.
2.  **Connection Settings**: Select **Cloud Pub/Sub** and paste the `topic_id` from Terraform output.

## 3. Deployment Configuration

Add the following to your `.env` (or Helm values):

```bash
# The subscription to pull from.
GOOGLE_CHAT_PUBSUB_SUBSCRIPTION=orrery-chat-events-sub

# The project hosting the subscription (if different from vertex/agent project).
GOOGLE_CHAT_PUBSUB_PROJECT=your-infra-project-id

# Performance tuning.
GOOGLE_CHAT_PUBSUB_MAX_MESSAGES=4
GOOGLE_CHAT_PUBSUB_HANDLER_TIMEOUT_SECONDS=600

# Health server (liveness /healthz, readiness /readyz).
# Readiness flips to 503 if the Pub/Sub streaming pull dies — kubelet
# will restart the pod. Default 8080 matches the Helm probe port.
GOOGLE_CHAT_PUBSUB_HEALTH_PORT=8080

# Idempotency guard (see "Idempotency" below).
#   memory   — process-local; single replica only.
#   postgres — shared via DATABASE_URL; required for >1 replica.
GOOGLE_CHAT_PUBSUB_IDEMPOTENCY_BACKEND=memory
GOOGLE_CHAT_PUBSUB_IDEMPOTENCY_TTL_SECONDS=3600
```

### Idempotency (why a redelivery won't double-act)

Pub/Sub delivers **at least once**. The same event can be redelivered when an
ack is lost to a network blip, a pod OOMs mid-callback, or a handler timeout
nacks after side effects already landed. Because the Chat handler can invoke
`@destructive` tools (restart / scale / rollback a deployment, increase Kafka
partitions, silence Alertmanager…), an unguarded redelivery would **run those
tools twice**.

The worker **claims each event id before dispatching** and drops duplicates
(ack without re-executing). If the handler then fails, the claim is **released**
so a legitimate redelivery still retries — you get at-most-once execution of
side effects without losing failed work.

| Backend | Use when | Notes |
|---------|----------|-------|
| `memory` | single replica (dev, or a pinned 1-replica prod) | Process-local; cannot dedup across pods. |
| `postgres` | **any multi-replica / autoscaled worker** | Shares the claim across replicas via the platform's `DATABASE_URL` (`INSERT … ON CONFLICT`). No extra infra. |

Set `GOOGLE_CHAT_PUBSUB_IDEMPOTENCY_TTL_SECONDS` to match the subscription's
`message_retention_duration` (default `3600s`) so a redelivery anywhere in the
retention window is still short-circuited. The Helm chart **refuses to render** a
worker with `replicaCount > 1` (or autoscaling on) while the backend is `memory`.

> **Timeout alignment**
> `GOOGLE_CHAT_PUBSUB_HANDLER_TIMEOUT_SECONDS` bounds a single *dispatch*;
> the subscriber's ack deadline is auto-extended while the callback runs, up
> to the subscription's `message_retention_duration`. Keep it **less than or
> equal to** `message_retention_duration` (default `3600s`).
>
> Agent turns (`MESSAGE`, `CARD_CLICKED`) are not bounded by it: the worker
> acks once the turn is scheduled and the reply is posted later through the
> Chat REST API. A turn is bounded by `GOOGLE_CHAT_TURN_TIMEOUT_SECONDS`
> (default `600`) instead — see [Turn timeout and shutdown](google-chat.md#turn-timeout-and-shutdown).

## 4. Run the Worker

The Pub/Sub worker runs as a separate process:

```bash
# Local (requires GOOGLE_APPLICATION_CREDENTIALS pointing to a SA key)
make run-chat MODE=pubsub

# In-cluster (enabled via Helm)
# pubsubWorker.enabled: true
```

## 5. Scaling the worker (multi-replica)

A single worker is a SPOF during incidents — Chat traffic spikes exactly when
things are on fire, and one pod serializes those turns behind `max_messages`.
To run more than one replica:

1. **Switch both process-local stores to the shared backend** so redeliveries
   are deduped — and pending approvals are visible — across pods:
   ```yaml
   pubsubWorker:
     idempotency:
       backend: postgres     # reuses the platform DATABASE_URL
     confirmation:
       backend: postgres     # same DATABASE_URL — approval handshake shared
   ```
   The confirmation store matters as much as the dedup guard: with the
   in-memory backend, the pending raised by pod A is invisible to pod B, so the
   operator's `approve` reply (or the LLM retry that consumes the approval)
   lands on a pod that knows nothing about it and the flow silently dies. The
   Postgres store also survives worker restarts — an in-memory pending dies
   with the pod. The chart refuses to render a multi-replica worker while
   either backend is `memory`. Traffic is human-scale (a handful of ~1 ms row
   operations per confirmation), so there is no measurable latency cost.
2. **Enable backlog-based autoscaling.** CPU/memory are poor signals here (the
   worker is I/O-bound on LLM calls); scale on the subscription's undelivered
   message count instead:
   ```yaml
   pubsubWorker:
     autoscaling:
       enabled: true
       minReplicas: 2
       maxReplicas: 6
       targetBacklogPerPod: 10   # scale out above ~10 unacked msgs/pod
   ```

**Prerequisite:** the backlog HPA reads an External metric
(`pubsub.googleapis.com|subscription|num_undelivered_messages`), which requires
the [GKE Stackdriver custom-metrics adapter](https://github.com/GoogleCloudPlatform/k8s-stackdriver/tree/master/custom-metrics-stackdriver-adapter).
Install it before enabling `pubsubWorker.autoscaling`.

> **Scaling lag.** The adapter polls ~every 60s, so backlog scaling is not
> instantaneous. During known incident windows keep `minReplicas >= 2` (or bump
> it on a schedule) rather than relying purely on reactive autoscaling.

## Troubleshooting Pub/Sub

1.  **Silence in logs**: Verify the subscription lives in the project specified by `GOOGLE_CHAT_PUBSUB_PROJECT`.
2.  **Permissions**: Ensure the GSA has `roles/pubsub.subscriber` on the subscription and is registered as the "App identity" in the Chat console.
3.  **Heartbeat**: With version 0.1.5+, the worker logs a `heartbeat` every 60s. If you don't see this, the process is likely stuck during initialization.
4.  **Duplicate actions**: If a destructive action ran twice, the idempotency guard is likely on `memory` across multiple replicas (only possible if you bypassed the Helm guard). Switch `GOOGLE_CHAT_PUBSUB_IDEMPOTENCY_BACKEND=postgres`. A log line `Duplicate event_id=… acking without re-executing` confirms the guard is working.
