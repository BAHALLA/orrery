"""FastAPI application with Slack bolt integration.

Exposes webhook endpoints for Slack Events API and interactive components.
Wires Slack events to the ADK Runner via SlackAgentHandler.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions.database_session_service import DatabaseSessionService
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp

from ai_agents_core import MetricsPlugin, authorize, default_plugins

from .config import SlackBotConfig
from .confirmation import ConfirmationStore, slack_confirmation
from .handler import APP_NAME, SlackAgentHandler
from .session_map import SessionMap

logger = logging.getLogger("slack_bot")

# ── Shared state ──────────────────────────────────────────────────────

config = SlackBotConfig()
store = ConfirmationStore()
session_map = SessionMap()

# Mutable ref updated per-message so the confirmation callback
# knows where to post buttons.
channel_ref: dict[str, str] = {"channel": "", "thread_ts": ""}


# ── Slack Bolt app ────────────────────────────────────────────────────

bolt_app = AsyncApp(
    token=config.slack_bot_token,
    signing_secret=config.slack_signing_secret,
)

# ── ADK setup (initialized at startup) ───────────────────────────────

_handler: SlackAgentHandler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize ADK runner on startup."""
    global _handler

    # Import agent here to avoid circular imports at module level
    from devops_assistant.agent import root_agent

    session_service = DatabaseSessionService(db_url=config.slack_db_url)

    # Slack-specific confirmation buttons are kept as agent-level callback.
    # Cross-cutting concerns (RBAC, metrics, audit, etc.) are handled by plugins.
    root_agent.before_tool_callback = [
        authorize(),
        slack_confirmation(
            store=store,
            slack_client=bolt_app.client,
            channel_ref=channel_ref,
        ),
    ]

    # Use default plugins but skip the guardrail gate (Slack has its own
    # confirmation flow via interactive buttons).
    plugins = default_plugins(guardrail_mode="none")
    adk_app = App(name=APP_NAME, root_agent=root_agent, plugins=plugins)
    runner = Runner(app=adk_app, session_service=session_service)

    _handler = SlackAgentHandler(
        runner=runner,
        session_service=session_service,
        session_map=session_map,
        channel_ref=channel_ref,
        config=config,
    )

    # Start Prometheus metrics server for scraping
    metrics_plugin = next((p for p in plugins if isinstance(p, MetricsPlugin)), None)
    if metrics_plugin:
        metrics_plugin.start_server(port=9100)

    logger.info("Slack bot started — ADK runner ready")
    yield
    logger.info("Slack bot shutting down")


# ── Event handlers ────────────────────────────────────────────────────


@bolt_app.event("message")
async def handle_message_event(event: dict, say):
    """Handle incoming Slack messages."""
    # Ignore bot messages and message edits/deletes
    if event.get("bot_id") or event.get("subtype"):
        return

    text = event.get("text", "").strip()
    if not text:
        return

    channel = event["channel"]
    # Use thread_ts if in a thread, otherwise use the message ts to start one
    thread_ts = event.get("thread_ts") or event["ts"]
    user_id = event.get("user", "unknown")

    if _handler is None:
        await say(text="Bot is still starting up, please try again.", thread_ts=thread_ts)
        return

    await _handler.handle_message(
        text=text,
        channel=channel,
        thread_ts=thread_ts,
        user_id=user_id,
        say=say,
    )


@bolt_app.event("app_mention")
async def handle_mention(event: dict, say):
    """Handle @bot mentions — same as message handler."""
    if event.get("bot_id"):
        return

    text = event.get("text", "").strip()
    # Remove the bot mention from the text
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

    if _handler is None:
        await say(text="Bot is still starting up, please try again.", thread_ts=thread_ts)
        return

    await _handler.handle_message(
        text=text,
        channel=channel,
        thread_ts=thread_ts,
        user_id=user_id,
        say=say,
    )


# ── Interactive component handlers (Approve/Deny buttons) ────────────


@bolt_app.action(re.compile(r"^confirm_"))
async def handle_approve(ack, action, say, body):
    """User clicked Approve on a guarded tool."""
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

    # Send a "yes, proceed" message to the runner so the LLM re-invokes the tool
    if _handler is None:
        return

    await _handler.handle_message(
        text=f"Yes, proceed with {confirmation.tool_name}.",
        channel=confirmation.channel,
        thread_ts=confirmation.thread_ts,
        user_id=confirmation.user_id,
        say=say,
    )


@bolt_app.action(re.compile(r"^deny_"))
async def handle_deny(ack, action, say, body):
    """User clicked Deny on a guarded tool."""
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

    # Tell the agent the operation was cancelled
    if _handler is None:
        return

    await _handler.handle_message(
        text=f"No, cancel {confirmation.tool_name}. Do not proceed.",
        channel=confirmation.channel,
        thread_ts=confirmation.thread_ts,
        user_id=confirmation.user_id,
        say=say,
    )


# ── FastAPI app ───────────────────────────────────────────────────────

api = FastAPI(title="DevOps Slack Bot", lifespan=lifespan)
slack_handler = AsyncSlackRequestHandler(bolt_app)


@api.post("/slack/events")
async def slack_events(request: Request):
    """Slack Events API and interactivity endpoint."""
    return await slack_handler.handle(request)


@api.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "handler_ready": _handler is not None}
