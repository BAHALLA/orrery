"""Resilience utilities: circuit breaker and retry with exponential backoff.

Provides two complementary patterns for fault tolerance:

- **CircuitBreaker**: Tracks per-tool failures across calls. When a tool exceeds
  the failure threshold, the circuit opens and subsequent calls are short-circuited
  with an error dict, giving the downstream service time to recover. After a
  recovery timeout, the circuit moves to half-open and allows one probe call.

- **with_retry**: Decorator for tool functions that adds automatic retry with
  exponential backoff and jitter for transient errors (ConnectionError,
  TimeoutError, etc.).

Both integrate with the ADK callback model:

    breaker = CircuitBreaker()

    create_agent(
        ...,
        before_tool_callback=[authorize(), breaker.before_tool_callback()],
        after_tool_callback=[audit_logger(), breaker.after_tool_callback()],
        on_tool_error_callback=breaker.on_tool_error_callback(),
    )

Tool-level retry:

    @with_retry(max_retries=3)
    def list_kafka_topics(timeout: int = 10) -> dict:
        ...
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import random
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any, TypeVar

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool

logger = logging.getLogger("orrery.resilience")

F = TypeVar("F", bound=Callable[..., Any])


# ── Circuit Breaker ──────────────────────────────────────────────────


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ToolOutcome(Enum):
    """How a completed tool call should move the circuit breaker.

    ``IGNORE`` exists because not every response describes the tool's health:
    a policy gate or the breaker itself can answer *instead of* the tool, and
    those must move the counters in neither direction.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    IGNORE = "ignore"


#: ``error_type`` values this breaker produces itself, short-circuiting the call
#: before the tool runs. Counting them would be self-reinforcing: every blocked
#: call would re-stamp ``_opened_at`` and the circuit could never time out into
#: half-open.
CIRCUIT_OPEN_ERROR = "CircuitOpen"
CIRCUIT_HALF_OPEN_ERROR = "CircuitHalfOpen"
_BREAKER_ERROR_TYPES = frozenset({CIRCUIT_OPEN_ERROR, CIRCUIT_HALF_OPEN_ERROR})

#: The repo-wide convention for "this tool call failed" (``validate_string`` and
#: every tool's ``except`` branch return it). Deliberately narrow: subsystem
#: health verdicts use their own vocabulary ("ok"/"fail"/"green") and describe
#: the *infrastructure*, not the call.
_FAILURE_STATUSES = frozenset({"error"})

#: Statuses meaning the call never reached the tool, so it says nothing about
#: whether the downstream service is up. Source of truth for each:
#: ``STATUS_BLOCKED`` / ``STATUS_AWAITING_CONFIRMATION`` in
#: ``plugins/autonomy_plugin.py``, ``access_denied`` in ``security/rbac.py``,
#: ``confirmation_required`` in ``security/guardrails.py``. Duplicated as
#: literals rather than imported to keep ``reliability`` free of a ``plugins``
#: import cycle; ``test_resilience.py`` asserts they stay in sync.
_NEUTRAL_STATUSES = frozenset(
    {
        "BLOCKED",
        "AWAITING_CONFIRMATION",
        "access_denied",
        "confirmation_required",
    }
)


def _response_field(response: Any, field: str) -> str | None:
    """Read *field* from a tool response, whatever shape it arrived in.

    ADK returns a tool's value verbatim — the ``{"result": ...}`` wrapping
    happens after the after-tool chain — so a response may be a dict, a
    Pydantic model (``ToolResult``), or a bare string. Only a string value
    counts; anything else is treated as absent.
    """
    value = response.get(field) if isinstance(response, dict) else getattr(response, field, None)
    return value if isinstance(value, str) else None


def classify_tool_outcome(response: Any) -> ToolOutcome:
    """Decide what a completed tool call means for the circuit breaker.

    Tools in this codebase catch their own exceptions and return
    ``{"status": "error", ...}`` rather than raising, so a response that merely
    *arrived* is not evidence of health — the outage case looks identical to
    the success case from ``on_tool_error_callback``'s point of view, because
    that callback never fires.

    A response with no recognisable ``status`` counts as success: it is the
    shape a tool returns when it has nothing to complain about.

    Note the deliberate false positive: a tool that answers "topic not found"
    with ``status: "error"`` is counted as a failure. Since the counter tracks
    *consecutive* failures and resets on the first success, this only opens a
    circuit when a tool returns nothing but errors ``failure_threshold`` times
    in a row — at which point backing off is the right call regardless of why.
    """
    status = _response_field(response, "status")
    if status is None:
        return ToolOutcome.SUCCESS
    if status in _NEUTRAL_STATUSES:
        return ToolOutcome.IGNORE
    if status in _FAILURE_STATUSES:
        if _response_field(response, "error_type") in _BREAKER_ERROR_TYPES:
            return ToolOutcome.IGNORE
        return ToolOutcome.FAILURE
    return ToolOutcome.SUCCESS


class CircuitBreaker:
    """Per-tool circuit breaker that integrates with ADK agent callbacks.

    Args:
        failure_threshold: Number of consecutive failures before opening the circuit.
        recovery_timeout: Seconds to wait before allowing a probe call (half-open).
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._half_open: dict[str, bool] = {}
        self._lock = threading.Lock()

    def state(self, tool_name: str) -> CircuitState:
        """Return the current circuit state for a tool."""
        with self._lock:
            return self._state_unlocked(tool_name)

    def _state_unlocked(self, tool_name: str) -> CircuitState:
        if tool_name not in self._opened_at:
            return CircuitState.CLOSED
        elapsed = time.monotonic() - self._opened_at[tool_name]
        if elapsed >= self._recovery_timeout:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def _record_failure(self, tool_name: str) -> None:
        with self._lock:
            count = self._failures.get(tool_name, 0) + 1
            self._failures[tool_name] = count
            if count >= self._failure_threshold:
                self._opened_at[tool_name] = time.monotonic()
                self._half_open[tool_name] = False
                logger.warning(
                    "Circuit OPEN for tool '%s' after %d consecutive failures",
                    tool_name,
                    count,
                )

    def _record_success(self, tool_name: str) -> None:
        with self._lock:
            was_open = tool_name in self._opened_at
            self._failures.pop(tool_name, None)
            self._opened_at.pop(tool_name, None)
            self._half_open.pop(tool_name, None)
            if was_open:
                logger.info("Circuit CLOSED for tool '%s' after successful probe", tool_name)

    def _clear_half_open_probe(self, tool_name: str) -> None:
        """Release a half-open probe slot for a call that never ran.

        ``before_tool_callback`` marks a probe in flight, but a gate that runs
        *after* this plugin can still answer instead of the tool — notably the
        per-agent ``require_confirmation()``, which is an agent callback rather
        than a plugin and so is not ordered ahead of it. Without this release
        the flag stays set, and every later call is refused as
        ``CircuitHalfOpen`` for a probe that will never report back.
        """
        with self._lock:
            self._half_open.pop(tool_name, None)

    def reset(self, tool_name: str | None = None) -> None:
        """Reset circuit state for a specific tool or all tools."""
        with self._lock:
            if tool_name:
                self._failures.pop(tool_name, None)
                self._opened_at.pop(tool_name, None)
                self._half_open.pop(tool_name, None)
            else:
                self._failures.clear()
                self._opened_at.clear()
                self._half_open.clear()

    def before_tool_callback(self) -> Callable:
        """Return a before_tool_callback that blocks calls when the circuit is open.

        When the circuit is open, returns an error dict so the LLM can reason
        about the outage. In half-open state, allows one probe call through.
        """
        breaker = self

        def callback(
            tool: BaseTool,
            args: dict[str, Any],
            tool_context: Context,
        ) -> dict | None:
            state = breaker.state(tool.name)
            if state == CircuitState.OPEN:
                logger.warning(
                    "Circuit breaker OPEN: blocking call to '%s'",
                    tool.name,
                )
                return {
                    "status": "error",
                    "error_type": CIRCUIT_OPEN_ERROR,
                    "message": (
                        f"Tool '{tool.name}' is temporarily unavailable due to repeated "
                        f"failures. It will be retried automatically after the recovery "
                        f"period ({breaker._recovery_timeout}s)."
                    ),
                }
            if state == CircuitState.HALF_OPEN:
                with breaker._lock:
                    if breaker._half_open.get(tool.name):
                        # Another half-open probe is already in flight
                        return {
                            "status": "error",
                            "error_type": CIRCUIT_HALF_OPEN_ERROR,
                            "message": (
                                f"Tool '{tool.name}' is being probed after an outage. "
                                f"Please wait for the probe to complete."
                            ),
                        }
                    breaker._half_open[tool.name] = True
                logger.info("Circuit HALF-OPEN: allowing probe call to '%s'", tool.name)
            return None

        return callback

    def after_tool_callback(self) -> Callable:
        """Return an after_tool_callback that records the call's outcome.

        A tool that *completed* has not necessarily *succeeded*: every tool here
        catches its own exceptions and returns ``{"status": "error"}``, so
        ``on_tool_error_callback`` almost never fires and this callback sees the
        outage. Recording success unconditionally would reset the failure count
        on exactly the calls the breaker exists to count, leaving it unable to
        open for the failure mode that matters. See :func:`classify_tool_outcome`.
        """
        breaker = self

        def callback(
            tool: BaseTool,
            args: dict[str, Any],
            tool_context: Context,
            tool_response: Any,
        ) -> dict | None:
            outcome = classify_tool_outcome(tool_response)
            if outcome is ToolOutcome.SUCCESS:
                breaker._record_success(tool.name)
            elif outcome is ToolOutcome.FAILURE:
                logger.debug(
                    "Tool '%s' returned an error status; counting as a circuit failure",
                    tool.name,
                )
                breaker._record_failure(tool.name)
            else:
                # The tool never ran (policy gate, or this breaker answered).
                breaker._clear_half_open_probe(tool.name)
            return None

        return callback

    def on_tool_error_callback(self) -> Callable:
        """Return an on_tool_error_callback that records failures.

        Returns None so other error callbacks (e.g., graceful_tool_error)
        can still produce the final response dict.
        """
        breaker = self

        def callback(
            tool: BaseTool,
            args: dict[str, Any],
            tool_context: Context,
            error: Exception,
        ) -> None:
            breaker._record_failure(tool.name)
            return None

        return callback


# ── Retry with Exponential Backoff ───────────────────────────────────

_DEFAULT_RETRYABLE = (ConnectionError, TimeoutError, OSError)


def with_retry(
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable: tuple[type[Exception], ...] = _DEFAULT_RETRYABLE,
) -> Callable[[F], F]:
    """Decorator that retries a tool function on transient errors.

    Uses exponential backoff with jitter: ``delay = min(base_delay * 2^attempt, max_delay)``
    multiplied by a random factor in [0.5, 1.0) to avoid thundering herd.

    Args:
        max_retries: Maximum number of retries (total attempts = max_retries + 1).
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Cap on the backoff delay.
        retryable: Tuple of exception types that trigger a retry.

    Usage::

        @with_retry(max_retries=3, retryable=(ConnectionError, TimeoutError))
        def list_kafka_topics(timeout: int = 10) -> dict:
            ...
    """

    def _compute_delay(attempt: int) -> float:
        delay = min(base_delay * (2**attempt), max_delay)
        return delay * (0.5 + random.random() * 0.5)  # noqa: S311

    def _log_retry(func_name: str, attempt: int, exc: Exception, jittered: float) -> None:
        logger.warning(
            "Retry %d/%d for '%s' after %s (%.1fs backoff)",
            attempt + 1,
            max_retries,
            func_name,
            type(exc).__name__,
            jittered,
        )

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                last_error: Exception | None = None
                for attempt in range(max_retries + 1):
                    try:
                        return await func(*args, **kwargs)
                    except retryable as exc:
                        last_error = exc
                        if attempt < max_retries:
                            jittered = _compute_delay(attempt)
                            _log_retry(
                                getattr(func, "__name__", repr(func)), attempt, exc, jittered
                            )
                            await asyncio.sleep(jittered)
                assert last_error is not None
                raise last_error

            return async_wrapper  # type: ignore

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_error: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable as exc:
                    last_error = exc
                    if attempt < max_retries:
                        jittered = _compute_delay(attempt)
                        _log_retry(getattr(func, "__name__", repr(func)), attempt, exc, jittered)
                        time.sleep(jittered)
            assert last_error is not None
            raise last_error

        return wrapper  # type: ignore

    return decorator
