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

A day is a UTC calendar day. Before a call goes out, an estimate of its
prompt (characters / 4) is *reserved*, so calls started together see each
other; after it, the response's ``usage_metadata`` total (prompt, output and
thinking) replaces the estimate. Final responses only: a streamed call's
partial chunks repeat the counts of the final one. Once a budget is spent,
``before_model_callback`` answers the call with a short model message, so no
tokens are bought to say so. What can still overshoot is the output of the
calls already in flight and the estimate's error.

Not charged: model calls made outside the plugin chain (the compaction
summarizer, knowledge embeddings, the onboarding self-test probe) and calls
that fail or are cancelled before their final response. The estimate leaves
out inline data and attachments, so a call carrying them reserves too little.

Counters live in memory per process by default. Pass a ``limits`` storage URI
(``redis://...``, the same one the HTTP rate limits use) to share them across
replicas. The store is a cost control, not a gate: when it fails or is slow,
the call goes ahead and the error is logged and counted.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Protocol

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from ..observability.metrics import track_token_budget_refusal, track_token_budget_store_error
from ..payload import text_volume

logger = logging.getLogger("orrery.plugins.token_budget")

#: Ledger key for the whole deployment; per-caller keys are ``user:<digest>``.
GLOBAL_KEY = "global"

#: Seconds a shared store may take before the call goes ahead without it.
_STORE_TIMEOUT_SECONDS = 2.0

#: Threads for a blocking store. Its own pool, never the default executor every
#: tool runs on: a hung store would otherwise strand the tools' threads too.
_STORE_WORKERS = 2

#: Reservations of calls in flight; one per agent run, so this is a leak bound.
_MAX_RESERVATIONS = 4096

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


def user_key(user_id: str) -> str:
    """The ledger key for *user_id*: bounded, printable, and not the subject itself."""
    return "user:" + hashlib.sha256(user_id.encode()).hexdigest()[:32]


def estimate_prompt_tokens(llm_request: LlmRequest) -> int:
    """A rough token count of what *llm_request* sends (about 4 characters each).

    Counts text, tool calls and results, the system instruction and the tool
    declarations. Not inline data, nor attachment content a later plugin adds.
    """
    chars = 0
    for content in llm_request.contents or []:
        for part in content.parts or []:
            chars += len(part.text or "")
            if part.function_call:
                chars += text_volume(part.function_call.args)
            if part.function_response:
                chars += text_volume(part.function_response.response)
    config = llm_request.config
    if config is not None:
        if isinstance(config.system_instruction, str):
            chars += len(config.system_instruction)
        for tool in config.tools or []:
            for declaration in getattr(tool, "function_declarations", None) or []:
                chars += text_volume(declaration.model_dump(exclude_none=True))
    return chars // 4


class UsageLedger(Protocol):
    """Tokens spent per key, per UTC day. ``charge`` takes a negative refund too."""

    #: Whether calls do I/O, and so run off the event loop under a timeout.
    blocking: bool

    def spent(self, key: str, day: str) -> int: ...

    def charge(self, key: str, day: str, tokens: int) -> None: ...


class InMemoryUsageLedger:
    """Per-process counters. Only today's survive a charge."""

    blocking = False

    def __init__(self) -> None:
        self._day = ""
        self._spent: dict[str, int] = {}
        self._lock = threading.Lock()

    def spent(self, key: str, day: str) -> int:
        with self._lock:
            return self._spent.get(key, 0) if day == self._day else 0

    def charge(self, key: str, day: str, tokens: int) -> None:
        with self._lock:
            if day < self._day:
                return  # a call that settled after midnight: its day is gone
            if day > self._day:
                # A new day: yesterday's keys go, so the map holds one day's callers.
                self._day, self._spent = day, {}
            self._spent[key] = self._spent.get(key, 0) + tokens


class StorageUsageLedger:
    """Counters in a ``limits`` storage (``redis://``, ``redis+sentinel://``, ...)."""

    blocking = True

    #: Kept a little past their day, then expired by the store itself.
    _EXPIRY_SECONDS = 2 * 24 * 3600

    def __init__(self, storage_uri: str) -> None:
        try:
            from limits.storage import Storage, storage_from_string
        except ImportError as exc:  # pragma: no cover — an install problem
            raise ImportError(
                "A shared token-budget store needs `limits`. Install with: uv sync --extra server"
            ) from exc
        # Without a socket timeout a hung server blocks the calling thread
        # forever; the plugin's own timeout only stops waiting for it.
        options = (
            {
                "socket_timeout": _STORE_TIMEOUT_SECONDS,
                "socket_connect_timeout": _STORE_TIMEOUT_SECONDS,
            }
            if storage_uri.startswith(("redis", "rediss"))
            else {}
        )
        storage = storage_from_string(storage_uri, **options)
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
        # (invocation, agent) -> (day, tokens reserved per key). An agent run
        # makes its model calls one at a time, so the pair names the call in
        # flight; one LlmAgent placed in two parallel branches of one Workflow
        # would share it, and nothing here does that.
        self._reserved: dict[tuple[str, str], tuple[str, dict[str, int]]] = {}
        self._executor = (
            ThreadPoolExecutor(max_workers=_STORE_WORKERS, thread_name_prefix="orrery-token-budget")
            if self._ledger.blocking
            else None
        )

    def _keys(self, user_id: str) -> list[str]:
        keys = [user_key(user_id)] if self.per_user else []
        return [*keys, GLOBAL_KEY] if self.total else keys

    async def _store(self, op: Callable[..., int | None], *args: object) -> tuple[bool, int]:
        """Run a ledger call: ``(succeeded, its value or 0)``; a failure is logged and counted."""
        try:
            if not self._ledger.blocking:
                return True, op(*args) or 0
            loop = asyncio.get_running_loop()
            value = await asyncio.wait_for(
                loop.run_in_executor(self._executor, op, *args), _STORE_TIMEOUT_SECONDS
            )
            return True, value or 0
        except Exception:
            track_token_budget_store_error()
            logger.exception(
                "Token budget store failed; letting the model call through",
                extra={"event": "token_budget_store_error"},
            )
            return False, 0

    async def _charge(self, keys: list[str], day: str, tokens: int) -> dict[str, int]:
        """Charge *tokens* to each key; returns what each key was actually charged."""
        charged: dict[str, int] = {}
        for key in keys:
            ok, _ = (
                await self._store(self._ledger.charge, key, day, tokens) if tokens else (True, 0)
            )
            charged[key] = tokens if ok else 0
        return charged

    async def _spent(self, key: str, day: str) -> int:
        return (await self._store(self._ledger.spent, key, day))[1]

    async def _exhausted(self, user_id: str, day: str) -> str | None:
        """Which budget is spent (``"global"``/``"user"``), if any."""
        if self.total and await self._spent(GLOBAL_KEY, day) >= self.total:
            return "global"
        if self.per_user and await self._spent(user_key(user_id), day) >= self.per_user:
            return "user"
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        user_id = callback_context.user_id
        day = self._today()
        scope = await self._exhausted(user_id, day)
        if scope is None:
            reserved = await self._charge(
                self._keys(user_id), day, estimate_prompt_tokens(llm_request)
            )
            if len(self._reserved) >= _MAX_RESERVATIONS:
                # A call that never reported back keeps its estimate charged;
                # only the bookkeeping is dropped. That is a call cancelled
                # mid-stream, or one a later before-model callback answered
                # (none does today), since neither reaches after_model.
                self._reserved.pop(next(iter(self._reserved)))
            self._reserved[self._call(callback_context)] = (day, reserved)
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

    @staticmethod
    def _call(callback_context: CallbackContext) -> tuple[str, str]:
        return (callback_context.invocation_id, callback_context.agent_name)

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        # With progressive SSE (ADK's default) a streamed call ends in exactly
        # one non-partial response carrying usage; the chunks before it repeat it.
        if llm_response.partial is True:
            return None
        day, reserved = self._reserved.pop(
            self._call(callback_context),
            (self._today(), dict.fromkeys(self._keys(callback_context.user_id), 0)),
        )
        usage = llm_response.usage_metadata
        if usage is None:
            return None  # the estimate stands
        # The total includes thinking tokens, which are billed like output.
        tokens = usage.total_token_count or (
            (usage.prompt_token_count or 0)
            + (usage.candidates_token_count or 0)
            + (usage.thoughts_token_count or 0)
        )
        if tokens > 0:
            for key, already in reserved.items():
                await self._charge([key], day, tokens - already)
        return None

    async def on_model_error_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest, error: Exception
    ) -> LlmResponse | None:
        # A failed call is not charged: refund its estimate. One that failed
        # after streaming some output was probably billed for its prompt, so
        # this leans toward undercounting.
        reservation = self._reserved.pop(self._call(callback_context), None)
        if reservation is not None:
            day, reserved = reservation
            for key, already in reserved.items():
                await self._charge([key], day, -already)
        return None
