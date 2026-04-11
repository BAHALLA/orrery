"""Slack bot configuration."""

from __future__ import annotations

import os

from ai_agents_core import AgentConfig


class SlackBotConfig(AgentConfig):
    """Configuration for the Slack bot integration.

    All values can be set via environment variables or a .env file.
    """

    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""  # only needed for Socket Mode
    slack_bot_port: int = 3000
    slack_db_url: str = "sqlite+aiosqlite:///slack_devops.db"

    # Rate limiting for the /slack/events endpoint (per source IP).
    # Format is slowapi-compatible, e.g. "60/minute" or "5/second".
    slack_rate_limit: str = "60/minute"

    # When running behind a reverse proxy (nginx ingress, GCP LB, ALB),
    # the connecting peer is the proxy, not the real client. Set this to
    # the number of trusted proxy hops to honor X-Forwarded-For. Leave at
    # 0 when exposed directly (the default, safer choice) — otherwise a
    # malicious client could spoof X-Forwarded-For and bypass the limit.
    slack_trusted_proxy_hops: int = 0

    def resolve_db_url(self) -> str:
        """Return the session-store URL, preferring ``DATABASE_URL`` if set.

        When the platform is running on Kubernetes with a shared Postgres,
        ``DATABASE_URL`` should be set and will take precedence over the
        legacy ``slack_db_url`` default.
        """
        return os.getenv("DATABASE_URL") or self.slack_db_url

    # RBAC: comma-separated Slack user IDs per role.
    # Users not listed default to "viewer".
    slack_admin_users: str = ""
    slack_operator_users: str = ""

    def resolve_role(self, user_id: str) -> str:
        """Return the role name for a Slack user ID."""
        admins = {u.strip() for u in self.slack_admin_users.split(",") if u.strip()}
        operators = {u.strip() for u in self.slack_operator_users.split(",") if u.strip()}
        if user_id in admins:
            return "admin"
        if user_id in operators:
            return "operator"
        return "viewer"
