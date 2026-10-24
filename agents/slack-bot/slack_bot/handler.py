"""Core message handler: Slack events → ADK Runner → Slack responses."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from orrery_core import AgentGateway, TurnTimedOut, progress_scope, set_user_role
from orrery_core.security.identity import CallerIdentity, enabled_systems
from orrery_core.serving.session_cache import BoundedSessionCache

from .config import SlackBotConfig
from .confirmation import slack_target
from .formatting import chunk_message, md_to_mrkdwn
from .progress import SlackProgress
from .session_map import SessionMap

logger = logging.getLogger("slack_bot.handler")

APP_NAME = "slack_devops"


class SlackAgentHandler:
    """Bridges Slack message events to the shared :class:`AgentGateway`.

    This is the Slack channel adapter: it maps a Slack thread to an ADK
    session (via :class:`SessionMap`), runs the turn through the gateway's
    shared pipeline, and renders the reply as Slack mrkdwn in-thread.
    """

    def __init__(
        self,
        gateway: AgentGateway,
        session_map: SessionMap,
        config: SlackBotConfig | None = None,
    ) -> None:
        self.gateway = gateway
        self.session_map = session_map
        self._config = config or SlackBotConfig()
        # Slack user id -> email, for impersonation. Bounded: a long-lived bot
        # sees an open-ended set of users.
        self._emails: BoundedSessionCache[str] = BoundedSessionCache(10_000)

    async def _identity(self, user_id: str, say: Any) -> CallerIdentity | None:
        """The sender as downstream systems know them, when impersonation is on.

        Slack's signed event vouches for the user *id*; Kubernetes and
        Elasticsearch know people by email, which ``users.info`` returns given
        the ``users:read.email`` scope. Looked up only when
        ``ORRERY_IMPERSONATION`` is set, and cached. A failed lookup yields no
        identity — impersonating systems then refuse the call rather than run
        it as the bot's service account.
        """
        if not enabled_systems():
            return None
        if (email := self._emails.get(user_id)) is None:
            client = getattr(say, "client", None)
            try:
                info = await client.users_info(user=user_id) if client is not None else None
                email = ((info or {}).get("user") or {}).get("profile", {}).get("email") or ""
            except Exception:
                logger.warning(
                    "Could not resolve the Slack user's email (users:read.email scope?)",
                    exc_info=True,
                )
                email = ""
            if email:
                self._emails.set(user_id, email)
        return CallerIdentity(subject=user_id, username=email) if email else None

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
        # Resolve the role on *every* turn so long-lived threads pick up
        # permission changes, then re-stamp it via state_delta below — matching
        # the Google Chat and HTTP surfaces. Without this, a thread's role would
        # be frozen at whatever it was when the session was first created.
        role = self._config.resolve_role(user_id)

        # Resolve or create this participant's ADK session for the thread. Keyed by
        # user as well as thread because ADK scopes sessions by
        # (app, user_id, session_id) — see SessionMap's docstring.
        session_id = self.session_map.get(channel, thread_ts, user_id)
        if session_id is None:
            session = await self.gateway.sessions.create_session(
                app_name=APP_NAME,
                user_id=user_id,
            )
            session_id = session.id
            self.session_map.set(channel, thread_ts, user_id, session_id)
            logger.info("New session for user=%s role=%s", user_id, role)

        # Per-turn identity: set_user_role stamps user_role + the server-trusted
        # lock flag so GuardrailsPlugin honors it instead of resetting to viewer.
        turn_state: dict[str, object] = {}
        set_user_role(turn_state, role)

        # Run the turn through the shared gateway pipeline. A slow turn gets a
        # status message edited as specialists work (see progress.py); the
        # progress scope is entered here, in the task that runs the agent, so
        # it reaches every specialist's sub-Runner.
        progress = SlackProgress(say=say, channel=channel, thread_ts=thread_ts)
        try:
            async with contextlib.AsyncExitStack() as stack:
                # This turn's approval cards go to this conversation.
                stack.enter_context(slack_target(channel, thread_ts))
                if self._config.slack_progress_updates and progress.enabled:
                    await stack.enter_async_context(progress)
                    stack.enter_context(progress_scope(progress.sink))
                reply = await self.gateway.run_in_session(
                    user_id=user_id,
                    session_id=session_id,
                    text=text,
                    state_delta=turn_state,
                    identity=await self._identity(user_id, say),
                )
        except TurnTimedOut:
            await say(
                text=(
                    "This took too long and was stopped. Some actions may already "
                    "have run, so check the current state before asking again."
                ),
                thread_ts=thread_ts,
            )
            return
        except Exception:
            logger.exception("Agent runner error")
            await say(
                text="Something went wrong while processing your request.",
                thread_ts=thread_ts,
            )
            return

        response_text = reply.text
        if not response_text:
            return

        # Convert markdown and send (chunked if long)
        formatted = md_to_mrkdwn(response_text)
        for chunk in chunk_message(formatted):
            await say(text=chunk, thread_ts=thread_ts)
