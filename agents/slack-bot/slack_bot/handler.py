"""Core message handler: Slack events → ADK Runner → Slack responses."""

from __future__ import annotations

import logging
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions.base_session_service import BaseSessionService
from google.genai import types

from ai_agents_core import set_user_role

from .config import SlackBotConfig
from .formatting import chunk_message, md_to_mrkdwn
from .session_map import SessionMap

logger = logging.getLogger("slack_bot.handler")

APP_NAME = "slack_devops"


class SlackAgentHandler:
    """Bridges Slack message events to the ADK Runner."""

    def __init__(
        self,
        runner: Runner,
        session_service: BaseSessionService,
        session_map: SessionMap,
        channel_ref: dict[str, str],
        config: SlackBotConfig | None = None,
    ) -> None:
        self.runner = runner
        self.session_service = session_service
        self.session_map = session_map
        self.channel_ref = channel_ref
        self._config = config or SlackBotConfig()

    async def handle_message(
        self,
        *,
        text: str,
        channel: str,
        thread_ts: str,
        user_id: str,
        say: Any,
    ) -> None:
        """Process a Slack message and respond in-thread.

        Args:
            text: The user's message text.
            channel: Slack channel ID.
            thread_ts: Thread timestamp (groups conversation).
            user_id: Slack user ID.
            say: Slack bolt's say() function for posting responses.
        """
        # Update channel_ref so the confirmation callback knows where to post
        self.channel_ref["channel"] = channel
        self.channel_ref["thread_ts"] = thread_ts

        # Resolve or create ADK session for this thread
        session_id = self.session_map.get(channel, thread_ts)
        if session_id is None:
            role = self._config.resolve_role(user_id)
            initial_state: dict[str, object] = {}
            set_user_role(initial_state, role)
            session = await self.session_service.create_session(
                app_name=APP_NAME,
                user_id=user_id,
                state=initial_state,
            )
            session_id = session.id
            self.session_map.set(channel, thread_ts, session_id)
            logger.info("New session for user=%s role=%s", user_id, role)

        message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=text)],
        )

        # Run the agent and collect the response
        response_text = ""
        try:
            async for event in self.runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message,
            ):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            response_text += part.text
        except Exception:
            logger.exception("Agent runner error")
            await say(
                text="Something went wrong while processing your request.",
                thread_ts=thread_ts,
            )
            return

        if not response_text:
            return

        # Convert markdown and send (chunked if long)
        formatted = md_to_mrkdwn(response_text)
        for chunk in chunk_message(formatted):
            await say(text=chunk, thread_ts=thread_ts)
