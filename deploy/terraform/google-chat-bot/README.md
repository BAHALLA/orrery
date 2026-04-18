# Google Chat bot — GCP infrastructure

Terraform module that provisions the Pub/Sub topic, subscription, and
Workload-Identity-bound service account consumed by
[`google_chat_bot.pubsub_worker`](../../../agents/google-chat-bot/google_chat_bot/pubsub_worker.py).

Use this module when you want the Chat bot to live on a private GKE
cluster — Pub/Sub avoids the need for a public ingress.

## What it creates

| Resource | Purpose |
|----------|---------|
| `google_pubsub_topic.events` | Topic the Chat app publishes events to. |
| `google_pubsub_topic_iam_member.chat_publisher` | Grants `roles/pubsub.publisher` on that topic to the SA shown in the Chat API console (overridable via `chat_publisher_email`). |
| `google_pubsub_subscription.events` | Pull subscription consumed by the worker. Retry policy + optional DLQ. |
| `google_pubsub_topic.dlq` *(optional)* | Dead-letter topic for poison messages. |
| `google_service_account.bot` | GSA the worker authenticates as (Pub/Sub subscriber + Chat app identity). |
| `google_pubsub_subscription_iam_member.bot_subscriber` | Grants `roles/pubsub.subscriber` on the events subscription to the GSA. |
| `google_service_account_iam_member.workload_identity` | Lets the in-cluster KSA impersonate the GSA. |

## What it does **not** do

Two steps still require the Google Chat API console (no Terraform
provider exists for them):

1. **Register the GSA as the Chat app identity** — Configuration tab →
   *App authentication* → paste the `gsa_email` output.
2. **Switch the app to Pub/Sub** — Configuration tab → *Connection
   settings* → choose *Cloud Pub/Sub* and paste the `topic_id` output.

## Usage
```hcl
module "orrery_chat_bot" {
  source = "../../deploy/terraform/google-chat-bot"

  project_id          = "project_id"
  k8s_namespace       = "namespace_name"
  k8s_service_account = "orrery-chat-bot"

  # Optional overrides:
  # name                = "orrery-chat"   # prefix for all resources
  # enable_dead_letter  = true            # default
  # max_delivery_attempts = 5

  # Override if your org enforces Domain Restricted Sharing (IAM policy).
  # For Workspace Add-ons, use the project's service agent identity:
  # chat_publisher_email = "service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
}
```
  # If your Chat app is a Workspace Add-on, or your org policy
  # (`constraints/iam.allowedPolicyMemberDomains`) blocks the default
  # `chat-api-push@system.gserviceaccount.com`, copy the exact SA shown
  # in the Chat API console → Configuration → Connection settings →
  # "Service Account Email", and pass it here:
  # chat_publisher_email = "service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
}

output "chat_bot_gsa" {
  value = module.orrery_chat_bot.gsa_email
}
```

After `terraform apply`:

```bash
terraform output gsa_email          # → orrery-chat-bot@PROJECT.iam.gserviceaccount.com
terraform output subscription_name  # → orrery-chat-events-sub
terraform output topic_id           # → projects/PROJECT/topics/orrery-chat-events
```

Wire these into the Helm chart at
[`deploy/helm/orrery-assistant`](../../helm/orrery-assistant/) via a values
override file (Helm is the only supported Kubernetes template set for this
project — the old `deploy/k8s/` manifests have been removed):

```yaml
# my-values.yaml
pubsubWorker:
  enabled: true
  serviceAccount:
    annotations:
      iam.gke.io/gcp-service-account: <gsa_email output>

config:
  GOOGLE_CHAT_PUBSUB_SUBSCRIPTION: <subscription_name output>
  GOOGLE_CLOUD_PROJECT: <project_id>
```

Then:

```bash
helm upgrade --install orrery deploy/helm/orrery-assistant -f my-values.yaml
```

Finally, in the Google Chat API console → Configuration tab, paste the
`gsa_email` output as the app identity and the `topic_id` output as the
Cloud Pub/Sub connection target.
