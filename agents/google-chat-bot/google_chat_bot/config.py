"""Configuration for the Google Chat bot."""

from __future__ import annotations

from ai_agents_core.config import AgentConfig


class GoogleChatBotConfig(AgentConfig):
    """Google Chat bot settings."""

    # Audience used to verify Google-signed ID tokens on the webhook. For a
    # Chat app configured with an HTTP endpoint, this is either the Google
    # Cloud project number or the endpoint URL — whichever you selected in
    # the Chat API console's "Authentication Audience" setting.
    google_chat_audience: str | None = None

    # Toggle token verification. Defaults to True for production; set to
    # False for local dev (e.g. ngrok without a real signed token).
    google_chat_verify_token: bool = True

    # RBAC — comma-separated email lists.
    google_chat_admin_emails: str = ""
    google_chat_operator_emails: str = ""

    # Valid identities for the Chat system service account — comma-separated.
    google_chat_identities: str = "chat-system@example.com"

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
