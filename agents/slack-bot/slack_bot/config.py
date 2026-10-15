"""Slack bot configuration."""

from __future__ import annotations

import os

from orrery_core import AgentConfig


class SlackBotConfig(AgentConfig):
    """Configuration for the Slack bot integration.

    All values can be set via environment variables or a .env file.
    """

    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""  # only needed for Socket Mode
    slack_bot_port: int = 3000
    # Empty → in-memory sessions. Set to a PostgreSQL URL (or DATABASE_URL) to
    # persist. SQLite is not supported.
    slack_db_url: str = ""

    # Rate limiting for the /slack/events endpoint (per source IP).
    # Rate-limit notation, e.g. "60/minute" or "5/second" (parsed by `limits`).
    slack_rate_limit: str = "60/minute"
    # Edit a status message in-thread while a slow turn runs (specialists
    # consulted, tool calls). Needs the bot's chat:write scope, which posting
    # replies already requires. SLACK_PROGRESS_UPDATES=false turns it off.
    slack_progress_updates: bool = True

    # When running behind a reverse proxy (nginx ingress, GCP LB, ALB),
    # the connecting peer is the proxy, not the real client. Set this to
    # the number of trusted proxy hops to honor X-Forwarded-For. Leave at
    # 0 when exposed directly (the default, safer choice) — otherwise a
    # malicious client could spoof X-Forwarded-For and bypass the limit.
    slack_trusted_proxy_hops: int = 0

    def resolve_db_url(self) -> str:
        """Return the session-store URL, preferring ``DATABASE_URL`` if set.

        When the platform is running on Kubernetes with a shared Postgres,
        ``DATABASE_URL`` should be set and takes precedence over ``slack_db_url``.
        Empty means an in-memory session store.
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
