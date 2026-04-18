# Google Chat: Pub/Sub Setup

The Pub/Sub transport is ideal for bots living in **private networks** (e.g., private GKE clusters)
where Google Chat cannot reach the bot via HTTP.

## Architecture

Google Chat publishes events to a **Pub/Sub Topic**, and the bot pulls from a **Subscription**.
The bot is outbound-only — no Ingress or public Load Balancer is required.

## 1. GCP Infrastructure (Terraform)

Use the provided module at [`deploy/terraform/google-chat-bot`](../../deploy/terraform/google-chat-bot/) to provision the required resources:

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
```

> **Timeout alignment**
> `GOOGLE_CHAT_PUBSUB_HANDLER_TIMEOUT_SECONDS` bounds a single turn; the
> subscriber's ack deadline is auto-extended while the callback runs, up
> to the subscription's `message_retention_duration`. Keep the handler
> timeout **less than or equal to** `message_retention_duration` (default
> `3600s`) so a stuck turn is reclaimed before the message expires.

## 4. Run the Worker

The Pub/Sub worker runs as a separate process:

```bash
# Local (requires GOOGLE_APPLICATION_CREDENTIALS pointing to a SA key)
make run-google-chat-pubsub

# In-cluster (enabled via Helm)
# pubsubWorker.enabled: true
```

## Troubleshooting Pub/Sub

1.  **Silence in logs**: Verify the subscription lives in the project specified by `GOOGLE_CHAT_PUBSUB_PROJECT`.
2.  **Permissions**: Ensure the GSA has `roles/pubsub.subscriber` on the subscription and is registered as the "App identity" in the Chat console.
3.  **Heartbeat**: With version 0.1.5+, the worker logs a `heartbeat` every 60s. If you don't see this, the process is likely stuck during initialization.
