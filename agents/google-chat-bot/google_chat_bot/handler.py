"""Bridge between Google Chat events and the ADK Agent Runner."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.adk.runners import Runner
from google.genai import types

from ai_agents_core import set_user_role

from .chat_client import ChatClient
from .config import GoogleChatBotConfig
from .confirmation import (
    ConfirmationStore,
    end_request_buffer,
    start_request_buffer,
)

logger = logging.getLogger("google_chat_bot.handler")

# Events that trigger a full agent run. These may exceed Google Chat's
# ~30 second synchronous budget and should be deferred to a background
# task when a ``ChatClient`` is available.
_LONG_RUNNING_EVENTS = {"MESSAGE", "CARD_CLICKED"}


def wrap_for_addons(text: str, cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Wrap a response in the Workspace Add-ons DataActions schema.

    When a bot is routed via the Add-ons infrastructure (gsuiteaddons),
    it expects a response matching the RenderActions or DataActions schema.
    To simply reply with a message, we use ``hostAppDataAction.chatDataAction``.
    """
    message: dict[str, Any] = {"text": text}
    if cards:
        message["cardsV2"] = cards
    return {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": message}}}}


def empty_ack() -> dict[str, Any]:
    """Return an empty async acknowledgement.

    Google Chat treats an empty ``hostAppDataAction`` as a no-op — no
    message is rendered to the user. The real reply is posted later
    via ``ChatClient.create_message``.
    """
    return {"hostAppDataAction": {}}


class GoogleChatHandler:
    """Handles incoming Google Chat events and delegates to an ADK Runner."""

    def __init__(
        self,
        runner: Runner,
        config: GoogleChatBotConfig,
        store: ConfirmationStore | None = None,
        chat_client: ChatClient | None = None,
    ):
        self.runner = runner
        self.config = config
        self.store = store or ConfirmationStore()
        # When chat_client is None, the handler stays in the legacy
        # synchronous path — useful for tests and local dev.
        self.chat_client = chat_client
        # Track fire-and-forget tasks so they don't get garbage-collected
        # before completion and so tests can await them.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def resolve_role(self, email: str) -> str:
        """Resolve RBAC role from user email (case-insensitive)."""
        normalized = (email or "").lower()
        if normalized in self.config.admin_emails:
            return "admin"
        if normalized in self.config.operator_emails:
            return "operator"
        return "viewer"

    async def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process a Google Chat event.

        Supports standard Chat API events and Workspace Add-ons events.
        """
        # 1. Standard Chat API uses top-level 'type'.
        event_type = event.get("type")

        # 2. Workspace Add-ons use a different structure (no top-level type).
        # We detect the type based on the payload structure.
        chat = event.get("chat") or {}
        common = event.get("commonEventObject") or {}

        # Detect MESSAGE
        if event_type == "MESSAGE" or chat.get("messagePayload"):
            if self._should_defer("MESSAGE"):
                self._spawn_background(self._handle_message_async(event))
                return empty_ack()
            return await self._handle_message(event)

        # Detect ADDED_TO_SPACE
        if event_type == "ADDED_TO_SPACE" or (chat.get("space") and not chat.get("messagePayload")):
            # Note: Add-ons often receive space info during the first interaction.
            return self._wrap_for_addons("Thanks for adding me! Mention me to start investigating.")

        # Detect CARD_CLICKED. Add-ons payloads don't carry a top-level "type",
        # so we fall back to the invokedFunction name that we set on our own
        # Approve/Deny buttons. We intentionally do NOT probe parameters here
        # because their shape varies (list of {key,value} dicts vs mapping).
        if event_type == "CARD_CLICKED" or common.get("invokedFunction") in (
            "confirm_action",
            "deny_action",
        ):
            if self._should_defer("CARD_CLICKED"):
                self._spawn_background(self._handle_card_click_async(event))
                return empty_ack()
            return await self._handle_card_click(event)

        logger.warning("Unrecognized event structure: %s", event)
        return self._wrap_for_addons("I'm not sure how to handle this event type.")

    # ── Internal helpers ─────────────────────────────────────────────

    def _wrap_for_addons(
        self, text: str, cards: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Instance alias for :func:`wrap_for_addons` — kept for convenience."""
        return wrap_for_addons(text, cards)

    def _should_defer(self, event_type: str) -> bool:
        """True when the event should run in a background task."""
        return self.chat_client is not None and event_type in _LONG_RUNNING_EVENTS

    def _spawn_background(self, coro: Any) -> asyncio.Task[Any]:
        """Schedule *coro* as a tracked background task."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _parse_message_event(self, event: dict[str, Any]) -> tuple[str, str, str, str | None]:
        """Extract ``(user_text, user_email, space_name, thread_name)``."""
        chat = event.get("chat") or {}
        msg_payload = chat.get("messagePayload") or {}
        message = event.get("message") or msg_payload.get("message") or {}

        # 1. User Text — from top-level or messagePayload.
        user_text = message.get("argumentText", "").strip()

        # 2. User Email — standard Chat path or Workspace Add-on path.
        user = event.get("user") or chat.get("user") or message.get("sender") or {}
        user_email = (user.get("email") or "unknown").lower()

        # 3. Space Name — check multiple paths (event, chat, or nested).
        space = event.get("space") or chat.get("space") or message.get("space") or {}
        space_name = space.get("name") or "default"

        # 4. Thread Name — if provided, message is a reply.
        thread = message.get("thread") or {}
        thread_name = thread.get("name")

        return user_text, user_email, space_name, thread_name

    async def _run_agent(
        self,
        *,
        session_id: str,
        user_id: str,
        user_text: str,
        user_role: str,
        space_name: str,
        thread_name: str | None,
        extra_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Drive a single agent turn and collect text + any buffered cards."""
        # NOTE: use ``set_user_role`` rather than a raw ``user_role`` write.
        # ``GuardrailsPlugin`` runs ``ensure_default_role()`` as a
        # before_agent_callback; it resets any ``user_role`` that wasn't
        # marked server-trusted back to ``viewer`` to prevent privilege
        # escalation from untrusted session state. ``set_user_role`` sets
        # both ``user_role`` and the ``_role_set_by_server`` lock flag so
        # the callback leaves it alone.
        state_delta: dict[str, Any] = {
            "gchat_space": space_name,
            "gchat_thread": thread_name or "",
        }
        set_user_role(state_delta, user_role)
        if extra_state:
            state_delta.update(extra_state)

        message = types.Content(role="user", parts=[types.Part.from_text(text=user_text)])

        cards, token = start_request_buffer()
        try:
            response_text = ""
            async for run_event in self.runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message,
                state_delta=state_delta,
            ):
                if run_event.content and run_event.content.parts:
                    for part in run_event.content.parts:
                        if part.text:
                            response_text += part.text
        finally:
            end_request_buffer(token)

        reply: dict[str, Any] = {}
        if response_text:
            reply["text"] = response_text
        if cards:
            reply["cardsV2"] = cards
        if not reply:
            reply["text"] = "(no response)"
        return reply

    async def _post_async_reply(
        self,
        *,
        space_name: str,
        thread_name: str | None,
        reply: dict[str, Any],
    ) -> None:
        """Post an agent reply via the Chat REST API."""
        if self.chat_client is None:
            logger.error("Cannot post async reply: chat_client is not configured")
            return

        if not space_name or space_name == "default":
            logger.warning("Cannot post async reply: valid space name was not found in event")
            return

        try:
            await self.chat_client.create_message(
                space_name,
                text=reply.get("text") or None,
                cards_v2=reply.get("cardsV2"),
                thread_name=thread_name,
            )
        except Exception:
            logger.exception("Failed to post async reply to %s", space_name)

    async def _post_async_error(self, space_name: str | None, thread_name: str | None) -> None:
        """Best-effort error notification when a background run crashes."""
        if self.chat_client is None or not space_name:
            return
        try:
            await self.chat_client.create_message(
                space_name,
                text="Sorry, I hit an unexpected error. Please try again.",
                thread_name=thread_name,
            )
        except Exception:
            logger.exception("Failed to post async error notification")

    # ── MESSAGE ───────────────────────────────────────────────────────

    async def _handle_message(self, event: dict[str, Any]) -> dict[str, Any]:
        user_text, user_email, space_name, thread_name = self._parse_message_event(event)

        if not user_text:
            return self._wrap_for_addons("How can I help you today?")

        session_id = f"gchat:{thread_name or space_name}"

        result = await self._run_agent(
            session_id=session_id,
            user_id=user_email,
            user_text=user_text,
            user_role=self.resolve_role(user_email),
            space_name=space_name,
            thread_name=thread_name,
        )

        return self._wrap_for_addons(result.get("text", "(no response)"), result.get("cardsV2"))

    async def _handle_message_async(self, event: dict[str, Any]) -> None:
        """Background-task counterpart to ``_handle_message``."""
        user_text, user_email, space_name, thread_name = self._parse_message_event(event)
        try:
            if not user_text:
                await self._post_async_reply(
                    space_name=space_name,
                    thread_name=thread_name,
                    reply={"text": "How can I help you today?"},
                )
                return

            session_id = f"gchat:{thread_name or space_name}"
            result = await self._run_agent(
                session_id=session_id,
                user_id=user_email,
                user_text=user_text,
                user_role=self.resolve_role(user_email),
                space_name=space_name,
                thread_name=thread_name,
            )
            await self._post_async_reply(
                space_name=space_name, thread_name=thread_name, reply=result
            )
        except Exception:
            logger.exception("Async message processing failed")
            await self._post_async_error(space_name, thread_name)

    # ── CARD_CLICKED ──────────────────────────────────────────────────

    def _parse_card_click_event(self, event: dict[str, Any]) -> tuple[str | None, str | None, str]:
        """Return ``(action_id, method, display_name)`` from a click event."""
        common = event.get("common") or event.get("commonEventObject") or {}
        action = event.get("action") or {}

        params = common.get("parameters") or action.get("parameters") or []
        if isinstance(params, list):
            params = {p.get("key"): p.get("value") for p in params if isinstance(p, dict)}

        method = common.get("invokedFunction") or action.get("actionMethodName")
        action_id = params.get("action_id") if isinstance(params, dict) else None

        chat = event.get("chat") or {}
        user = event.get("user") or chat.get("user") or {}
        display_name = user.get("displayName") or user.get("email") or "unknown"

        return action_id, method, display_name

    def _build_click_synthetic(
        self, pending: Any, method: str, display_name: str
    ) -> tuple[str, dict[str, Any], str] | None:
        """Derive ``(synthetic_text, extra_state, ack_text)`` for a click.

        Returns ``None`` if the method is unrecognized.
        """
        if method == "confirm_action":
            return (
                f"Yes, proceed with {pending.tool_name}.",
                {},
                f"*Approved* by {display_name} — executing `{pending.tool_name}`",
            )
        if method == "deny_action":
            return (
                f"No, cancel {pending.tool_name}. Do not proceed.",
                {f"_gchat_pending_{pending.tool_name}": False},
                f"*Denied* by {display_name} — `{pending.tool_name}` was not executed.",
            )
        return None

    async def _handle_card_click(self, event: dict[str, Any]) -> dict[str, Any]:
        """Handle Approve/Deny button clicks from confirmation cards."""
        action_id, method, display_name = self._parse_card_click_event(event)

        if not action_id or not method:
            logger.warning("CARD_CLICKED missing action_id or method")
            return self._wrap_for_addons("This card action is not recognized.")

        pending = self.store.pop(action_id)
        if pending is None:
            return self._wrap_for_addons("This action has expired or was already processed.")

        synthetic = self._build_click_synthetic(pending, method, display_name)
        if synthetic is None:
            return self._wrap_for_addons(f"Unknown action: {method}")
        synthetic_text, extra_state, ack_text = synthetic

        result = await self._run_agent(
            session_id=pending.session_id,
            user_id=pending.user_id,
            user_text=synthetic_text,
            user_role=self.resolve_role(pending.user_id),
            space_name=pending.space_name,
            thread_name=pending.thread_name,
            extra_state=extra_state,
        )

        combined_text = ack_text
        if result.get("text"):
            combined_text = f"{ack_text}\n\n{result['text']}"

        return self._wrap_for_addons(combined_text, result.get("cardsV2"))

    async def _handle_card_click_async(self, event: dict[str, Any]) -> None:
        """Background-task counterpart to ``_handle_card_click``."""
        action_id, method, display_name = self._parse_card_click_event(event)

        if not action_id or not method:
            logger.warning("CARD_CLICKED missing action_id or method")
            # We don't know where to post, so just drop it. The top-level
            # handler returned an ack already, so the UI is consistent.
            return

        pending = self.store.pop(action_id)
        if pending is None:
            space_name = self._click_space(event)
            if space_name:
                await self._post_async_reply(
                    space_name=space_name,
                    thread_name=self._click_thread(event),
                    reply={"text": "This action has expired or was already processed."},
                )
            return

        synthetic = self._build_click_synthetic(pending, method, display_name)
        if synthetic is None:
            await self._post_async_reply(
                space_name=pending.space_name,
                thread_name=pending.thread_name,
                reply={"text": f"Unknown action: {method}"},
            )
            return
        synthetic_text, extra_state, ack_text = synthetic

        try:
            result = await self._run_agent(
                session_id=pending.session_id,
                user_id=pending.user_id,
                user_text=synthetic_text,
                user_role=self.resolve_role(pending.user_id),
                space_name=pending.space_name,
                thread_name=pending.thread_name,
                extra_state=extra_state,
            )

            combined_text = ack_text
            if result.get("text"):
                combined_text = f"{ack_text}\n\n{result['text']}"

            await self._post_async_reply(
                space_name=pending.space_name,
                thread_name=pending.thread_name,
                reply={"text": combined_text, "cardsV2": result.get("cardsV2")},
            )
        except Exception:
            logger.exception("Async card click processing failed")
            await self._post_async_error(pending.space_name, pending.thread_name)

    @staticmethod
    def _click_space(event: dict[str, Any]) -> str | None:
        chat = event.get("chat") or {}
        space = event.get("space") or chat.get("space") or {}
        return space.get("name")

    @staticmethod
    def _click_thread(event: dict[str, Any]) -> str | None:
        message = event.get("message") or {}
        thread = message.get("thread") or {}
        return thread.get("name")
