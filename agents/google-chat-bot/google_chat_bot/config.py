"""Configuration for the Google Chat bot."""

from __future__ import annotations

from orrery_core.config import AgentConfig


class GoogleChatBotConfig(AgentConfig):
    """Google Chat bot settings."""

    # Audience used to verify Google-signed ID tokens on the webhook. For
    # HTTP-endpoint Chat apps the audience is *always* the endpoint URL
    # (byte-for-byte, including the trailing slash) — the "project number"
    # audience option only applies to Pub/Sub / Apps Script / Dialogflow
    # connection types. Ignored when the bot runs in Pub/Sub mode.
    google_chat_audience: str | None = None

    # Toggle token verification. Defaults to True for production; set to
    # False for local dev (e.g. ngrok without a real signed token).
    google_chat_verify_token: bool = True

    # RBAC — comma-separated email lists.
    google_chat_admin_emails: str = ""
    google_chat_operator_emails: str = ""

    # Valid identities for the Chat system service account — comma-separated.
    google_chat_identities: str = "chat@system.gserviceaccount.com"

    # Asynchronous response mode. When True, the webhook returns 200 OK
    # immediately for MESSAGE / CARD_CLICKED events and posts the real
    # reply back via the Chat REST API — required for agent runs that
    # exceed Google Chat's ~30 second synchronous budget. When False,
    # the handler keeps the sync path (simpler local dev, but any run
    # longer than 30s will time out on the Chat UI).
    google_chat_async_response: bool = True

    # Optional service-account JSON file used to authenticate async
    # REST API posts. If unset, Application Default Credentials are
    # used (``GOOGLE_APPLICATION_CREDENTIALS`` or
    # ``gcloud auth application-default login``).
    google_chat_service_account_file: str | None = None

    # ── Pub/Sub transport ─────────────────────────────────────────────
    # When the bot lives in a private network (e.g. private GKE) that
    # Google Chat cannot reach over HTTP, configure the Chat app to
    # publish events to a Pub/Sub topic and run ``pubsub_worker`` to
    # pull from a subscription on that topic. Only the worker reads
    # these settings — the FastAPI HTTP transport ignores them.

    # Subscription identifier. Either a short ID (``orrery-chat-events``)
    # or a fully qualified path (``projects/X/subscriptions/Y``). When
    # only the short form is provided, ``google_chat_pubsub_project`` —
    # falling back to ``GOOGLE_CLOUD_PROJECT`` — is used to qualify it.
    google_chat_pubsub_subscription: str | None = None

    # Project hosting the Pub/Sub subscription. Defaults to
    # ``GOOGLE_CLOUD_PROJECT`` when unset.
    google_chat_pubsub_project: str | None = None

    # Maximum number of messages held concurrently by the subscriber.
    # Each in-flight message keeps a callback thread busy, so this
    # also bounds parallel agent runs. Tune alongside the subscription's
    # ack-deadline and your CPU/memory budget.
    google_chat_pubsub_max_messages: int = 4

    # Per-message handler timeout (seconds). Pub/Sub auto-extends the
    # ack deadline while a callback is running, but we still cap the
    # individual handler so a wedged turn cannot pin a thread forever.
    # Defaults to 10 minutes — long enough for multi-step remediation,
    # short enough that a stuck run is reclaimed in reasonable time.
    google_chat_pubsub_handler_timeout_seconds: int = 600

    @property
    def valid_identities(self) -> frozenset[str]:
        return frozenset(
            [
                identity.strip().lower()
                for identity in self.google_chat_identities.split(",")
                if identity.strip()
            ]
        )

    @property
    def admin_emails(self) -> list[str]:
        return [
            email.strip().lower()
            for email in self.google_chat_admin_emails.split(",")
            if email.strip()
        ]

    @property
    def operator_emails(self) -> list[str]:
        return [
            email.strip().lower()
            for email in self.google_chat_operator_emails.split(",")
            if email.strip()
        ]
