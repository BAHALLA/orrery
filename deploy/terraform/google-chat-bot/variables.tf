variable "project_id" {
  description = "GCP project that hosts the Pub/Sub topic, subscription, and GSA."
  type        = string
}

variable "name" {
  description = "Logical name used to derive resource names (topic, subscription, GSA)."
  type        = string
  default     = "orrery-chat"
}

variable "k8s_namespace" {
  description = "Kubernetes namespace running the chat bot worker."
  type        = string
}

variable "k8s_service_account" {
  description = "Kubernetes ServiceAccount the chat bot worker runs as."
  type        = string
  default     = "orrery-chat-bot"
}

variable "vertex_ai_project_id" {
  description = "GCP project where Vertex AI (Gemini) is called. Defaults to var.project_id."
  type        = string
  default     = null
}

variable "enable_vertex_ai" {
  description = "Grant Vertex AI User permissions to the bot GSA. Set to false if using a different LLM provider (OpenAI, Anthropic)."
  type        = bool
  default     = true
}


variable "ack_deadline_seconds" {
  description = <<-EOT
    Initial ack deadline for the subscription. The subscriber client
    auto-extends this while a callback is running, so the value mainly
    affects how quickly an unprocessed message is redelivered when the
    pod dies mid-flight. Keep aligned with the slowest expected agent
    turn so a healthy worker never trips redelivery.
  EOT
  type        = number
  default     = 60
}

variable "message_retention_duration" {
  description = "How long Pub/Sub keeps unacked messages. Defaults to one hour."
  type        = string
  default     = "3600s"
}

variable "max_delivery_attempts" {
  description = <<-EOT
    Max delivery attempts before a message is routed to the dead-letter
    topic (when enable_dead_letter is true). Set to 0 to disable.
  EOT
  type        = number
  default     = 5
}

variable "enable_dead_letter" {
  description = "Create and wire a dead-letter topic for poison messages."
  type        = bool
  default     = true
}

variable "dlq_subscribers" {
  description = <<-EOT
    IAM members granted `roles/pubsub.subscriber` on the DLQ subscription
    for triage (pulling and inspecting poison messages). Accepts any
    member syntax Pub/Sub supports, e.g.:

      ["group:sre-oncall@example.com", "user:alice@example.com"]

    Ignored when `enable_dead_letter` is false.
  EOT
  type        = list(string)
  default     = []
}

variable "chat_publisher_email" {
  description = <<-EOT
    Service account that Google Chat uses to publish events to the topic.
    Grab the exact value from the Chat API console → Configuration tab →
    Connection settings → "Service Account Email" once you select Cloud
    Pub/Sub. Varies by app type:

      • Classic Chat app: chat-api-push@system.gserviceaccount.com
      • Workspace Add-on: service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com

    The default assumes a classic Chat app. If your org enforces
    `constraints/iam.allowedPolicyMemberDomains`, the add-on SA (a
    GCP-managed service agent in your own project) is usually the only
    one that can be bound without an org-policy exception.
  EOT
  type        = string
  default     = "chat-api-push@system.gserviceaccount.com"
}

variable "labels" {
  description = "Labels applied to all created resources."
  type        = map(string)
  default = {
    component  = "orrery-chat-bot"
    managed-by = "terraform"
  }
}
