"""Slack bot configuration."""

from __future__ import annotations

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
