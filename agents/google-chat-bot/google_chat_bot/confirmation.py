"""Google Chat confirmation flow for guarded tools.

When a guarded tool fires, ``google_chat_confirmation`` short-circuits the
call, appends a Card v2 to a request-scoped buffer, and records the pending
action in a ``ConfirmationStore``. The handler returns the buffered card as
part of the synchronous webhook response — no Chat REST client needed. When
the user clicks Approve/Deny the handler looks up the action and re-enters
the runner with a synthetic user message so the LLM can retry (or cancel).

This mirrors the Slack bot's confirmation pattern so operators see a
consistent UX across transports.
"""

from __future__ import annotations

import contextvars
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

from ai_agents_core import get_guard_level, get_guard_reason

from .cards import build_confirmation_card

# Per-request buffer for cards emitted by before_tool_callback. The handler
# sets this at the start of each webhook request; the callback appends to it
# and the handler returns the contents in the response.
_pending_cards: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "_gchat_pending_cards", default=None
)


@dataclass
class PendingConfirmation:
    """Stores context for a tool awaiting user approval."""

    action_id: str
    tool_name: str
    user_id: str
    session_id: str
    space_name: str
    thread_name: str | None
    level: str


@dataclass
class ConfirmationStore:
    """Thread-safe store of pending confirmations keyed by action_id."""

    _pending: dict[str, PendingConfirmation] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, confirmation: PendingConfirmation) -> None:
        with self._lock:
            self._pending[confirmation.action_id] = confirmation

    def pop(self, action_id: str) -> PendingConfirmation | None:
        with self._lock:
            return self._pending.pop(action_id, None)

    def get(self, action_id: str) -> PendingConfirmation | None:
        with self._lock:
            return self._pending.get(action_id)


def start_request_buffer() -> tuple[list[dict[str, Any]], contextvars.Token]:
    """Begin a fresh card buffer for the current request context."""
    buf: list[dict[str, Any]] = []
    token = _pending_cards.set(buf)
    return buf, token


def end_request_buffer(token: contextvars.Token) -> None:
    """Tear down the per-request card buffer."""
    _pending_cards.reset(token)


def _push_card(card: dict[str, Any]) -> bool:
    """Append a card to the active request buffer. Returns False if none."""
    buf = _pending_cards.get()
    if buf is None:
        return False
    buf.append(card)
    return True


def google_chat_confirmation(store: ConfirmationStore) -> Callable:
    """Create a ``before_tool_callback`` that emits approval cards.

    Args:
        store: Shared :class:`ConfirmationStore` used to resume runs when the
            user clicks a button.
    """

    def callback(*, tool: BaseTool, args: dict[str, Any], tool_context: Context) -> dict | None:
        func = getattr(tool, "func", None)
        if func is None:
            return None

        level = get_guard_level(func)
        if level is None:
            return None  # not guarded, proceed

        # Second turn after user-approved retry: consume the flag and proceed.
        pending_key = f"_gchat_pending_{tool.name}"
        if tool_context.state.get(pending_key):
            tool_context.state[pending_key] = False
            return None

        # First turn: record pending state and emit a card.
        tool_context.state[pending_key] = True

        reason = get_guard_reason(func)
        action_id = uuid.uuid4().hex[:12]

        session_id = (
            tool_context.session.id
            if hasattr(tool_context, "session") and tool_context.session
            else "unknown"
        )
        user_id = getattr(tool_context, "user_id", "unknown")
        space_name = tool_context.state.get("gchat_space", "")
        thread_name = tool_context.state.get("gchat_thread") or None

        store.add(
            PendingConfirmation(
                action_id=action_id,
                tool_name=tool.name,
                user_id=user_id,
                session_id=session_id,
                space_name=space_name,
                thread_name=thread_name,
                level=level,
            )
        )

        card = build_confirmation_card(tool.name, args, reason, level, action_id)
        buffered = _push_card(card)

        reason_msg = f" This action {reason}." if reason else ""
        notice = (
            "An approval card has been posted — click Approve or Deny."
            if buffered
            else "Approval is required from an operator."
        )
        return {
            "status": "confirmation_required",
            "message": (
                f"The tool '{tool.name}' requires confirmation.{reason_msg} "
                f"{notice} Waiting for user response."
            ),
        }

    return callback
