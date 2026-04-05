"""Run the Slack bot in Socket Mode (no public URL required).

Socket Mode connects outbound to Slack via WebSocket, so it works
behind firewalls and NATs — ideal for local development and testing.

Requires a SLACK_APP_TOKEN (xapp-...) in addition to the bot token.

Usage:
    uv run python -m slack_bot.socket_mode
"""

from __future__ import annotations

import asyncio
import logging
import re

from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions.database_session_service import DatabaseSessionService
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from ai_agents_core import authorize, default_plugins

from .config import SlackBotConfig
from .confirmation import ConfirmationStore, slack_confirmation
from .handler import APP_NAME, SlackAgentHandler
from .session_map import SessionMap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("slack_bot")

# ── Shared state ──────────────────────────────────────────────────────

config = SlackBotConfig()
store = ConfirmationStore()
session_map = SessionMap()
channel_ref: dict[str, str] = {"channel": "", "thread_ts": ""}


def _create_bolt_app() -> AsyncApp:
    """Create and configure the Slack Bolt app with all event handlers."""
    bolt_app = AsyncApp(token=config.slack_bot_token)

    _handler_ref: dict[str, SlackAgentHandler | None] = {"handler": None}

    @bolt_app.event("message")
    async def handle_message_event(event: dict, say):
        if event.get("bot_id") or event.get("subtype"):
            return
        text = event.get("text", "").strip()
        if not text:
            return

        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "unknown")
        handler = _handler_ref["handler"]

        if handler is None:
            await say(text="Bot is still starting up, please try again.", thread_ts=thread_ts)
            return

        await handler.handle_message(
            text=text, channel=channel, thread_ts=thread_ts, user_id=user_id, say=say
        )

    @bolt_app.event("app_mention")
    async def handle_mention(event: dict, say):
        if event.get("bot_id"):
            return
        text = event.get("text", "").strip()
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
        if not text:
            await say(
                text="How can I help? Ask me about Kafka, Kubernetes, Docker, or run a system triage.",
                thread_ts=event.get("thread_ts") or event["ts"],
            )
            return

        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "unknown")
        handler = _handler_ref["handler"]

        if handler is None:
            await say(text="Bot is still starting up, please try again.", thread_ts=thread_ts)
            return

        await handler.handle_message(
            text=text, channel=channel, thread_ts=thread_ts, user_id=user_id, say=say
        )

    @bolt_app.action(re.compile(r"^confirm_"))
    async def handle_approve(ack, action, say, body):
        await ack()
        action_id = action["value"]
        confirmation = store.pop(action_id)
        if confirmation is None:
            return

        approver = body.get("user", {}).get("username", "unknown")
        await say(
            text=f":white_check_mark: *Approved* by @{approver} — executing `{confirmation.tool_name}`",
            thread_ts=confirmation.thread_ts,
            channel=confirmation.channel,
        )

        handler = _handler_ref["handler"]
        if handler is None:
            return
        await handler.handle_message(
            text=f"Yes, proceed with {confirmation.tool_name}.",
            channel=confirmation.channel,
            thread_ts=confirmation.thread_ts,
            user_id=confirmation.user_id,
            say=say,
        )

    @bolt_app.action(re.compile(r"^deny_"))
    async def handle_deny(ack, action, say, body):
        await ack()
        action_id = action["value"]
        confirmation = store.pop(action_id)
        if confirmation is None:
            return

        denier = body.get("user", {}).get("username", "unknown")
        await say(
            text=f":no_entry_sign: *Denied* by @{denier} — `{confirmation.tool_name}` was not executed.",
            thread_ts=confirmation.thread_ts,
            channel=confirmation.channel,
        )

        handler = _handler_ref["handler"]
        if handler is None:
            return
        await handler.handle_message(
            text=f"No, cancel {confirmation.tool_name}. Do not proceed.",
            channel=confirmation.channel,
            thread_ts=confirmation.thread_ts,
            user_id=confirmation.user_id,
            say=say,
        )

    return bolt_app, _handler_ref


async def main() -> None:
    """Start the Slack bot in Socket Mode."""
    if not config.slack_app_token:
        logger.error(
            "SLACK_APP_TOKEN is required for Socket Mode. "
            "Generate one at https://api.slack.com/apps → Basic Information → App-Level Tokens "
            "(scope: connections:write)"
        )
        return

    if not config.slack_bot_token:
        logger.error("SLACK_BOT_TOKEN is required.")
        return

    bolt_app, handler_ref = _create_bolt_app()

    # Initialize ADK
    from devops_assistant.agent import root_agent

    session_service = DatabaseSessionService(db_url=config.slack_db_url)

    root_agent.before_tool_callback = [
        authorize(),
        slack_confirmation(
            store=store,
            slack_client=bolt_app.client,
            channel_ref=channel_ref,
        ),
    ]

    app = App(
        name=APP_NAME,
        root_agent=root_agent,
        plugins=default_plugins(guardrail_mode="none"),
    )
    runner = Runner(app=app, session_service=session_service)

    handler_ref["handler"] = SlackAgentHandler(
        runner=runner,
        session_service=session_service,
        session_map=session_map,
        channel_ref=channel_ref,
        config=config,
    )

    logger.info("Starting Slack bot in Socket Mode...")
    socket_handler = AsyncSocketModeHandler(bolt_app, config.slack_app_token)
    await socket_handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
