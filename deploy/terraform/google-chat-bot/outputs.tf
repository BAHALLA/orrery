output "topic_name" {
  description = "Pub/Sub topic the Chat app must publish events to."
  value       = google_pubsub_topic.events.name
}

output "topic_id" {
  description = "Fully qualified topic ID — paste into the Chat app Configuration tab."
  value       = google_pubsub_topic.events.id
}

output "subscription_name" {
  description = "Short subscription ID — set as GOOGLE_CHAT_PUBSUB_SUBSCRIPTION."
  value       = google_pubsub_subscription.events.name
}

output "subscription_id" {
  description = "Fully qualified subscription ID."
  value       = google_pubsub_subscription.events.id
}

output "gsa_email" {
  description = "Google Service Account email — set as pubsubWorker.serviceAccount.annotations.\"iam.gke.io/gcp-service-account\" in the Helm chart values AND register as the Chat app identity."
  value       = google_service_account.bot.email
}

output "dead_letter_topic_name" {
  description = "Dead-letter topic name (null when disabled)."
  value       = var.enable_dead_letter ? google_pubsub_topic.dlq[0].name : null
}
