"""TokenBudgetPlugin — cap the model tokens a caller, and a deployment, spend per day.

A request rate limit bounds how often someone may *start* a turn, not what the
turn costs: one message can fan out to every specialist, each run re-sends its
history on every model call, and a leaked or shared token can keep that up all
day. On a deployment someone pays for (a public lab, a team's API key), that
is an open credit line.

Two daily ceilings, each off by default:

* **per caller** — the ADK ``user_id`` (the verified subject on the console and
  A2A; an agent acting for a user spends that user's budget);
* **per deployment** — everyone together, the backstop that fails closed when
  the per-caller budgets are too generous for the bill.

A day is a UTC calendar day. Tokens are charged in ``after_model_callback``
from the response's ``usage_metadata`` (its total: prompt, output and thinking), on final responses
only: a streamed call's partial chunks repeat the counts of the final one. A
call is refused in ``before_model_callback`` once a budget is spent, by
answering it with a short model message, so no tokens are bought to say so;
the call that crosses the line is allowed to finish, so a budget can be
overshot by at most one model call per concurrent run.

Counters live in memory per process by default. Pass a ``limits`` storage URI
(``redis://...``, the same one the HTTP rate limits use) to share them across
replicas.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from ..observability.metrics import track_token_budget_refusal

logger = logging.getLogger("orrery.plugins.token_budget")

#: Ledger key for the whole deployment. Not a valid subject on any surface.
GLOBAL_KEY = "\0all"

_USER_MESSAGE = (
    "You have reached today's usage limit for this assistant. It resets at "
    "00:00 UTC; until then I can't take on new work."
)
_GLOBAL_MESSAGE = (
    "This assistant has reached its usage limit for today. It resets at "
    "00:00 UTC; please try again then."
)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class UsageLedger(Protocol):
    """Tokens spent per key, per UTC day."""

    def spent(self, key: str, day: str) -> int: ...

    def charge(self, key: str, day: str, tokens: int) -> None: ...


class InMemoryUsageLedger:
    """Per-process counters. Only today's survive a charge."""

    def __init__(self) -> None:
        self._day = ""
        self._spent: dict[str, int] = {}
        self._lock = threading.Lock()

    def spent(self, key: str, day: str) -> int:
        with self._lock:
            return self._spent.get(key, 0) if day == self._day else 0

    def charge(self, key: str, day: str, tokens: int) -> None:
        with self._lock:
            if day != self._day:
                # A new day: yesterday's keys go, so the map holds one day's callers.
                self._day, self._spent = day, {}
            self._spent[key] = self._spent.get(key, 0) + tokens


class StorageUsageLedger:
    """Counters in a ``limits`` storage (``redis://``, ``memcached://``, ...)."""

    #: Kept a little past their day, then expired by the store itself.
    _EXPIRY_SECONDS = 2 * 24 * 3600

    def __init__(self, storage_uri: str) -> None:
        try:
            from limits.storage import Storage, storage_from_string
        except ImportError as exc:  # pragma: no cover — an install problem
            raise ImportError(
                "A shared token-budget store needs `limits`. Install with: uv sync --extra server"
            ) from exc
        storage = storage_from_string(storage_uri)
        if not isinstance(storage, Storage):
            # The plugin's callbacks count synchronously, like the rate limits.
            raise ValueError(f"{storage_uri!r} is an async storage; use a synchronous one")
        self._storage = storage

    @staticmethod
    def _key(key: str, day: str) -> str:
        return f"orrery:token-budget:{day}:{key}"

    def spent(self, key: str, day: str) -> int:
        return int(self._storage.get(self._key(key, day)) or 0)

    def charge(self, key: str, day: str, tokens: int) -> None:
        self._storage.incr(self._key(key, day), self._EXPIRY_SECONDS, amount=tokens)


def create_usage_ledger(storage_uri: str | None) -> UsageLedger:
    """A shared ledger for a non-memory *storage_uri*, else a per-process one."""
    if not storage_uri or storage_uri.startswith("memory://"):
        return InMemoryUsageLedger()
    return StorageUsageLedger(storage_uri)


class TokenBudgetPlugin(BasePlugin):
    """Refuse model calls once a caller's or the deployment's daily tokens are spent.

    Args:
        per_user: Tokens one ``user_id`` may spend per UTC day; ``0`` disables.
        total: Tokens the whole deployment may spend per UTC day; ``0`` disables.
        ledger: Where spending is counted (default: per process).
        today: The current day's key; injectable for tests.
    """

    def __init__(
        self,
        *,
        per_user: int = 0,
        total: int = 0,
        ledger: UsageLedger | None = None,
        today: Callable[[], str] = _today,
    ) -> None:
        super().__init__(name="token_budget")
        if per_user < 0 or total < 0:
            raise ValueError("token budgets must be >= 0 (0 disables)")
        self.per_user = per_user
        self.total = total
        self._ledger = ledger or InMemoryUsageLedger()
        self._today = today

    def _exhausted(self, user_id: str, day: str) -> str | None:
        """Which budget is spent (``"global"``/``"user"``), if any."""
        if self.total and self._ledger.spent(GLOBAL_KEY, day) >= self.total:
            return "global"
        if self.per_user and self._ledger.spent(user_id, day) >= self.per_user:
            return "user"
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        user_id = callback_context.user_id
        scope = self._exhausted(user_id, self._today())
        if scope is None:
            return None
        track_token_budget_refusal(scope)
        logger.warning(
            "Daily token budget spent (%s); refusing a model call",
            scope,
            extra={"event": "token_budget_exhausted", "scope": scope, "user_id": user_id},
        )
        message = _GLOBAL_MESSAGE if scope == "global" else _USER_MESSAGE
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part.from_text(text=message)])
        )

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        if llm_response.partial is True:
            return None
        usage = llm_response.usage_metadata
        if usage is None:
            return None
        # The total includes thinking tokens, which are billed like output.
        tokens = usage.total_token_count or (
            (usage.prompt_token_count or 0)
            + (usage.candidates_token_count or 0)
            + (usage.thoughts_token_count or 0)
        )
        if tokens <= 0:
            return None
        day = self._today()
        if self.per_user:
            self._ledger.charge(callback_context.user_id, day, tokens)
        if self.total:
            self._ledger.charge(GLOBAL_KEY, day, tokens)
        return None
