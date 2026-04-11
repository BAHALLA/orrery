"""Google Chat REST API client for asynchronous message posting.

Google Chat enforces a ~30-second synchronous budget on webhook
responses. For agent runs that exceed that budget (multi-step
remediation, restart + verify loops, parallel triage), the webhook
must return ``200 OK`` immediately and post the real reply back via
``spaces.messages.create``. This module encapsulates that post path.

The client authenticates via the Chat Bot scope. Credentials come
from either an explicit service-account file or Application Default
Credentials (``GOOGLE_APPLICATION_CREDENTIALS`` or
``gcloud auth application-default login``). The service account
identity must be granted the Chat Bot API on the Chat API console.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger("google_chat_bot.chat_client")

CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
_BASE_URL = "https://chat.googleapis.com/v1"
_REQUEST_TIMEOUT_SECONDS = 30.0


class ChatClient:
    """Minimal async Google Chat REST client for asynchronous replies."""

    def __init__(self, credentials: Any):
        self._credentials = credentials

    @classmethod
    def from_adc(cls) -> ChatClient:
        """Build a client using Application Default Credentials."""
        import google.auth

        credentials, _ = google.auth.default(scopes=[CHAT_BOT_SCOPE])
        return cls(credentials)

    @classmethod
    def from_service_account_file(cls, path: str) -> ChatClient:
        """Build a client from a service-account JSON file."""
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            path, scopes=[CHAT_BOT_SCOPE]
        )
        return cls(credentials)

    async def _get_token(self) -> str:
        """Return a valid access token, refreshing on demand.

        ``Credentials.refresh`` is synchronous, so we offload it to a
        worker thread to avoid blocking the event loop.
        """
        if not self._credentials.valid:
            await asyncio.to_thread(self._credentials.refresh, GoogleAuthRequest())
        return self._credentials.token

    async def create_message(
        self,
        space_name: str,
        *,
        text: str | None = None,
        cards_v2: list[dict[str, Any]] | None = None,
        thread_name: str | None = None,
    ) -> dict[str, Any]:
        """Post a new message to ``space_name``, optionally in a thread.

        Args:
            space_name: Fully qualified space resource name, e.g.
                ``spaces/AAA``.
            text: Plain-text body. At least one of ``text`` or
                ``cards_v2`` must be provided.
            cards_v2: Optional list of Card v2 entries
                (``{"cardId": ..., "card": ...}``) to render alongside
                the text.
            thread_name: If provided, the message is posted as a reply
                in this thread; otherwise it starts a new one.

        Returns:
            The parsed JSON response from the Chat REST API.
        """
        if not text and not cards_v2:
            raise ValueError("create_message requires text or cards_v2")

        body: dict[str, Any] = {}
        if text:
            body["text"] = text
        if cards_v2:
            body["cardsV2"] = cards_v2
        if thread_name:
            body["thread"] = {"name": thread_name}

        url = f"{_BASE_URL}/{space_name}/messages"
        params: dict[str, str] = {}
        if thread_name:
            # Fall back to a new thread if the original one has been
            # deleted or is otherwise unreachable.
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=body, params=params, headers=headers)
            if resp.status_code >= 400:
                logger.error(
                    "Chat REST API error %s posting to %s: %s",
                    resp.status_code,
                    space_name,
                    resp.text,
                )
                resp.raise_for_status()
            return resp.json()
