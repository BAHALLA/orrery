"""Configuration for the Google Chat bot."""

from __future__ import annotations

from orrery_core.agent.config import AgentConfig


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

    # Confirmation store backend (platform-wide ORRERY_CONFIRMATION_BACKEND):
    # 'memory' (single replica only — pendings are process-local and die with
    # the pod) or 'postgres' (shared across replicas + survives restarts, via
    # DATABASE_URL). Must be 'postgres' whenever the Pub/Sub worker can run
    # more than one replica.
    orrery_confirmation_backend: str = "memory"

    # Render clickable Approve/Deny (and Run-remediation) buttons on cards.
    # A button click is a CARD_CLICKED interaction that Google's add-ons
    # runtime resolves with a *synchronous* HTTPS round-trip — which only the
    # HTTP-endpoint transport can provide. On the Pub/Sub transport there is
    # no synchronous channel, so a click fails on Google's side with
    # "the Chat app didn't respond or its response was invalid" (error code 3)
    # before the worker can help it. Default False (Pub/Sub-safe): cards ask
    # the operator to *reply* "approve" / "deny" in the card's thread instead —
    # replies are plain MESSAGE events, which Pub/Sub always delivers with the
    # thread attached. Set True only for HTTP-endpoint deployments.
    google_chat_interactive_buttons: bool = False

    # Wall-clock budget for one agent turn (seconds; 0 disables). A deferred
    # turn runs in a background task after its event was already acknowledged,
    # so nothing else bounds it: without this, a wedged run leaves its thread on
    # "Investigating…" forever. On expiry the progress card is replaced with a
    # notice listing any guarded changes that went through before the stop.
    google_chat_turn_timeout_seconds: float = 600

    # On shutdown, how long in-flight turns get to finish before they are
    # stopped (seconds). Keep it under the pod's terminationGracePeriodSeconds.
    google_chat_shutdown_grace_seconds: float = 20

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
    # This bounds *dispatch*, not agent runs: MESSAGE and CARD_CLICKED events
    # are handed to a background task and acknowledged straight away (the
    # Chat REST client posts the reply later), so a run's duration is bounded
    # by ``google_chat_turn_timeout_seconds``, not by this setting.
    google_chat_pubsub_max_messages: int = 4

    # Per-message handler timeout (seconds). Pub/Sub auto-extends the
    # ack deadline while a callback is running, but we still cap the
    # individual handler so a wedged dispatch cannot pin a thread forever.
    # Agent turns themselves run deferred and are bounded separately by
    # ``google_chat_turn_timeout_seconds``.
    google_chat_pubsub_handler_timeout_seconds: int = 600

    # Idempotency guard. Pub/Sub is at-least-once, so a redelivered event
    # would double-run @destructive tools. The worker claims each event id
    # before dispatching and drops duplicates. ``memory`` is process-local
    # (single replica only); ``postgres`` shares the claim across replicas
    # via the platform's DATABASE_URL and is required for replicaCount > 1.
    google_chat_pubsub_idempotency_backend: str = "memory"

    # Claim TTL (seconds). Match the subscription's message_retention_duration
    # so a redelivery anywhere in the retention window is still short-circuited.
    google_chat_pubsub_idempotency_ttl_seconds: int = 3600

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
