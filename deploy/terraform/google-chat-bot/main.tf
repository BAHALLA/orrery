# Google Chat bot — Pub/Sub + Workload Identity wiring.
#
# This module provisions the GCP-side resources the bot needs to receive
# Chat events on a private GKE cluster:
#
#   • A Pub/Sub topic that the Google Chat service publishes events to.
#   • A pull subscription consumed by google_chat_bot.pubsub_worker.
#   • An optional dead-letter topic + subscription for poison messages.
#   • A Google Service Account (GSA) that:
#       - Holds roles/pubsub.subscriber on the subscription.
#       - Is bound via Workload Identity to the Kubernetes
#         ServiceAccount running the worker pod.
#
# The Chat publisher binding (chat-api-push@system.gserviceaccount.com →
# roles/pubsub.publisher) is granted on the topic so Google's own
# infrastructure can deliver events.
#
# What this module does NOT do:
#
#   • Register the GSA as the Chat app identity. That step lives in the
#     Google Chat API console (Configuration → App authentication) and
#     cannot be expressed in Terraform today.
#   • Toggle the Chat app's "Connection settings" to Pub/Sub. Same UI,
#     same limitation.
#
# Run `terraform output` after apply for the GSA email — paste it into
# `pubsubWorker.serviceAccount.annotations."iam.gke.io/gcp-service-account"`
# in deploy/helm/orrery-assistant/values.yaml (Helm is the only supported
# Kubernetes template set for this project).

locals {
  topic_name        = "${var.name}-events"
  subscription_name = "${var.name}-events-sub"
  dlq_topic_name    = "${var.name}-events-dlq"
  dlq_subscription  = "${var.name}-events-dlq-sub"
  gsa_account_id    = "${var.name}-bot"
}

# ── Pub/Sub topic that Google Chat publishes events to ────────────────
resource "google_pubsub_topic" "events" {
  project = var.project_id
  name    = local.topic_name
  labels  = var.labels
}

# Allow the Google Chat infrastructure to publish to the topic. This
# binding is mandatory — without it, the Chat API cannot deliver events.
resource "google_pubsub_topic_iam_member" "chat_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.events.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${var.chat_publisher_email}"
}

# ── Optional dead-letter topic ────────────────────────────────────────
resource "google_pubsub_topic" "dlq" {
  count   = var.enable_dead_letter ? 1 : 0
  project = var.project_id
  name    = local.dlq_topic_name
  labels  = var.labels
}

# Diagnostics-only subscription so a human can pull from the DLQ.
resource "google_pubsub_subscription" "dlq" {
  count   = var.enable_dead_letter ? 1 : 0
  project = var.project_id
  name    = local.dlq_subscription
  topic   = google_pubsub_topic.dlq[0].name
  labels  = var.labels

  message_retention_duration = "604800s" # 7 days, max useful for triage.
  ack_deadline_seconds       = 60
}

# ── Main subscription consumed by the worker ──────────────────────────
resource "google_pubsub_subscription" "events" {
  project = var.project_id
  name    = local.subscription_name
  topic   = google_pubsub_topic.events.name
  labels  = var.labels

  ack_deadline_seconds       = var.ack_deadline_seconds
  message_retention_duration = var.message_retention_duration

  # Exponential backoff on nack — a transient failure (LLM rate-limit,
  # database hiccup) gets retried with increasing delay rather than
  # hammering the bot.
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dynamic "dead_letter_policy" {
    for_each = var.enable_dead_letter ? [1] : []
    content {
      dead_letter_topic     = google_pubsub_topic.dlq[0].id
      max_delivery_attempts = var.max_delivery_attempts
    }
  }

  expiration_policy {
    # Never expire — the subscription is owned by this module.
    ttl = ""
  }
}

# Pub/Sub needs explicit permission on the DLQ topic so it can publish
# expired messages there. Without this binding, redelivery silently
# loops instead of moving the message to the DLQ.
resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  count   = var.enable_dead_letter ? 1 : 0
  project = var.project_id
  topic   = google_pubsub_topic.dlq[0].name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

data "google_project" "this" {
  project_id = var.project_id
}

# ── Google Service Account for the worker ─────────────────────────────
resource "google_service_account" "bot" {
  project      = var.project_id
  account_id   = local.gsa_account_id
  display_name = "Orrery Google Chat bot (Pub/Sub worker)"
  description  = "Workload identity for the Pub/Sub-based Google Chat bot. Must also be registered as the Chat app identity in the Chat API console."
}

# Subscriber permission on the events subscription only — narrowest
# scope that lets the worker pull and ack messages.
resource "google_pubsub_subscription_iam_member" "bot_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.events.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.bot.email}"
}

# Allow the bot to call Vertex AI (Gemini). Optional — only needed if
# Gemini is used as the LLM provider.
resource "google_project_iam_member" "vertex_ai_user" {
  count   = var.enable_vertex_ai ? 1 : 0
  project = coalesce(var.vertex_ai_project_id, var.project_id)
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.bot.email}"
}

# ── Workload Identity binding ─────────────────────────────────────────
# Allow the in-cluster KSA to impersonate this GSA. The Chat bot pod's
# google-auth library picks this up automatically through the GKE
# metadata server when ADC is requested.
resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.bot.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.k8s_namespace}/${var.k8s_service_account}]"
}
