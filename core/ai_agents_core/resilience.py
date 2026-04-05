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

logger = logging.getLogger("ai_agents.resilience")

F = TypeVar("F", bound=Callable[..., Any])


# ── Circuit Breaker ──────────────────────────────────────────────────


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


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
                    "error_type": "CircuitOpen",
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
                            "error_type": "CircuitHalfOpen",
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
        """Return an after_tool_callback that records successful tool calls."""
        breaker = self

        def callback(
            tool: BaseTool,
            args: dict[str, Any],
            tool_context: Context,
            tool_response: dict,
        ) -> dict | None:
            breaker._record_success(tool.name)
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
                            _log_retry(func.__name__, attempt, exc, jittered)
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
                        _log_retry(func.__name__, attempt, exc, jittered)
                        time.sleep(jittered)
            assert last_error is not None
            raise last_error

        return wrapper  # type: ignore

    return decorator
