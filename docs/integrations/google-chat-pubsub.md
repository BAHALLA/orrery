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
- The Pub/Sub **Topic** and **Subscription**.
- A **Google Service Account (GSA)** with subscriber permissions.
- **Workload Identity** bindings for GKE.

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
```

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
