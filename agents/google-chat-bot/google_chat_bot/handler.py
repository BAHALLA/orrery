"""Bridge between Google Chat events and the ADK Agent Runner."""

from __future__ import annotations

import logging
from typing import Any

from google.adk.runners import Runner
from google.genai import types

from .config import GoogleChatBotConfig
from .confirmation import (
    ConfirmationStore,
    end_request_buffer,
    start_request_buffer,
)

logger = logging.getLogger("google_chat_bot.handler")


class GoogleChatHandler:
    """Handles incoming Google Chat events and delegates to an ADK Runner."""

    def __init__(
        self,
        runner: Runner,
        config: GoogleChatBotConfig,
        store: ConfirmationStore | None = None,
    ):
        self.runner = runner
        self.config = config
        self.store = store or ConfirmationStore()

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
            return await self._handle_message(event)

        # Detect ADDED_TO_SPACE
        if event_type == "ADDED_TO_SPACE" or (chat.get("space") and not chat.get("messagePayload")):
            # Note: Add-ons often receive space info during the first interaction.
            return self._wrap_for_addons("Thanks for adding me! Mention me to start investigating.")

        # Detect CARD_CLICKED
        # Add-ons interact via commonEventObject.parameters or action.
        if event_type == "CARD_CLICKED" or common.get("parameters", {}).get("action_id"):
            return await self._handle_card_click(event)

        logger.warning("Unrecognized event structure: %s", event)
        return self._wrap_for_addons("I'm not sure how to handle this event type.")

    # ── Internal helpers ─────────────────────────────────────────────

    def _wrap_for_addons(
        self, text: str, cards: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Wrap a response in the Workspace Add-ons DataActions schema.

        When a bot is routed via the Add-ons infrastructure (gsuiteaddons),
        it expects a response matching the RenderActions or DataActions schema.
        To simply reply with a message, we use hostAppDataAction.chatDataAction.
        """
        message: dict[str, Any] = {"text": text}
        if cards:
            message["cardsV2"] = cards

        # Workspace Add-ons standard for creating a message in response to a webhook.
        return {
            "hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": message}}}
        }

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
        state_delta: dict[str, Any] = {
            "user_role": user_role,
            "gchat_space": space_name,
            "gchat_thread": thread_name or "",
        }
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

    async def _handle_message(self, event: dict[str, Any]) -> dict[str, Any]:
        chat = event.get("chat") or {}
        msg_payload = chat.get("messagePayload") or {}

        # Standard Chat API path
        if not msg_payload:
            user_text = event.get("message", {}).get("argumentText", "").strip()
            user_email = (event.get("user", {}).get("email") or "unknown").lower()
            space_name = event.get("space", {}).get("name", "default")
            thread_name = event.get("message", {}).get("thread", {}).get("name")
        else:
            # Workspace Add-on path
            user_text = msg_payload.get("message", {}).get("argumentText", "").strip()
            user_email = (chat.get("user", {}).get("email") or "unknown").lower()
            space_name = chat.get("space", {}).get("name", "default")
            thread_name = msg_payload.get("message", {}).get("thread", {}).get("name")

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

    async def _handle_card_click(self, event: dict[str, Any]) -> dict[str, Any]:
        """Handle Approve/Deny button clicks from confirmation cards."""
        # Detect parameters from Standard API or Add-on commonEventObject
        common = event.get("common") or event.get("commonEventObject") or {}
        action = event.get("action") or {}

        # Add-ons use commonEventObject.parameters
        params = common.get("parameters") or action.get("parameters") or []
        if isinstance(params, list):
            params = {p.get("key"): p.get("value") for p in params if isinstance(p, dict)}

        method = common.get("invokedFunction") or action.get("actionMethodName")

        action_id = params.get("action_id")
        if not action_id or not method:
            logger.warning("CARD_CLICKED missing action_id or method")
            return self._wrap_for_addons("This card action is not recognized.")

        pending = self.store.pop(action_id)
        if pending is None:
            return self._wrap_for_addons("This action has expired or was already processed.")

        # Resolve user info
        chat = event.get("chat") or {}
        user = event.get("user") or chat.get("user") or {}
        display_name = user.get("displayName") or user.get("email") or "unknown"

        if method == "confirm_action":
            synthetic_text = f"Yes, proceed with {pending.tool_name}."
            extra_state: dict[str, Any] = {}
            ack_text = f"*Approved* by {display_name} — executing `{pending.tool_name}`"
        elif method == "deny_action":
            synthetic_text = f"No, cancel {pending.tool_name}. Do not proceed."
            # Clear pending flag so a retry won't bypass the guard silently.
            extra_state = {f"_gchat_pending_{pending.tool_name}": False}
            ack_text = f"*Denied* by {display_name} — `{pending.tool_name}` was not executed."
        else:
            return self._wrap_for_addons(f"Unknown action: {method}")

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
