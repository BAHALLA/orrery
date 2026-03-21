"""Unit tests for resilience utilities (circuit breaker + retry)."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from ai_agents_core.resilience import (
    CircuitBreaker,
    CircuitState,
    with_retry,
)

# ── CircuitBreaker state transitions ─────────────────────────────────


class TestCircuitBreakerState:
    def test_starts_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        assert cb.state("tool_a") == CircuitState.CLOSED

    def test_stays_closed_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb._record_failure("tool_a")
        cb._record_failure("tool_a")
        assert cb.state("tool_a") == CircuitState.CLOSED

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb._record_failure("tool_a")
        assert cb.state("tool_a") == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb._record_failure("tool_a")
        cb._record_failure("tool_a")
        cb._record_success("tool_a")
        cb._record_failure("tool_a")
        # Only 1 failure since last success
        assert cb.state("tool_a") == CircuitState.CLOSED

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0)
        cb._record_failure("tool_a")
        cb._record_failure("tool_a")
        assert cb.state("tool_a") == CircuitState.OPEN

        # Simulate time passing
        with patch("ai_agents_core.resilience.time") as mock_time:
            mock_time.monotonic.return_value = cb._opened_at["tool_a"] + 11.0
            assert cb.state("tool_a") == CircuitState.HALF_OPEN

    def test_success_after_half_open_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb._record_failure("tool_a")
        cb._record_failure("tool_a")
        assert cb.state("tool_a") == CircuitState.OPEN

        cb._record_success("tool_a")
        assert cb.state("tool_a") == CircuitState.CLOSED

    def test_independent_per_tool(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb._record_failure("tool_a")
        cb._record_failure("tool_a")
        assert cb.state("tool_a") == CircuitState.OPEN
        assert cb.state("tool_b") == CircuitState.CLOSED

    def test_reset_single_tool(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb._record_failure("tool_a")
        cb._record_failure("tool_a")
        cb._record_failure("tool_b")
        cb._record_failure("tool_b")

        cb.reset("tool_a")
        assert cb.state("tool_a") == CircuitState.CLOSED
        assert cb.state("tool_b") == CircuitState.OPEN

    def test_reset_all_tools(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb._record_failure("tool_a")
        cb._record_failure("tool_a")
        cb._record_failure("tool_b")
        cb._record_failure("tool_b")

        cb.reset()
        assert cb.state("tool_a") == CircuitState.CLOSED
        assert cb.state("tool_b") == CircuitState.CLOSED


# ── CircuitBreaker ADK callbacks ─────────────────────────────────────


class TestCircuitBreakerCallbacks:
    def _make_tool(self, name: str) -> MagicMock:
        tool = MagicMock()
        tool.name = name
        return tool

    def test_before_callback_allows_when_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        before = cb.before_tool_callback()
        result = before(self._make_tool("t"), {}, MagicMock())
        assert result is None

    def test_before_callback_blocks_when_open(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb._record_failure("t")
        cb._record_failure("t")

        before = cb.before_tool_callback()
        result = before(self._make_tool("t"), {}, MagicMock())
        assert result is not None
        assert result["error_type"] == "CircuitOpen"

    def test_before_callback_allows_probe_in_half_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.0)
        cb._record_failure("t")
        cb._record_failure("t")
        # recovery_timeout=0 means it's immediately half-open

        before = cb.before_tool_callback()
        result = before(self._make_tool("t"), {}, MagicMock())
        assert result is None  # probe allowed

    def test_before_callback_blocks_second_probe_in_half_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.0)
        cb._record_failure("t")
        cb._record_failure("t")

        before = cb.before_tool_callback()
        # First probe allowed
        before(self._make_tool("t"), {}, MagicMock())
        # Second probe blocked
        result = before(self._make_tool("t"), {}, MagicMock())
        assert result is not None
        assert result["error_type"] == "CircuitHalfOpen"

    def test_after_callback_records_success(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb._record_failure("t")
        cb._record_failure("t")
        assert cb.state("t") == CircuitState.OPEN

        after = cb.after_tool_callback()
        result = after(self._make_tool("t"), {}, MagicMock(), {"status": "ok"})
        assert result is None  # doesn't override response
        assert cb.state("t") == CircuitState.CLOSED

    def test_error_callback_records_failure(self):
        cb = CircuitBreaker(failure_threshold=2)
        on_error = cb.on_tool_error_callback()

        on_error(self._make_tool("t"), {}, MagicMock(), ConnectionError("down"))
        assert cb.state("t") == CircuitState.CLOSED  # 1 failure, threshold=2

        on_error(self._make_tool("t"), {}, MagicMock(), ConnectionError("down"))
        assert cb.state("t") == CircuitState.OPEN

    def test_error_callback_returns_none(self):
        """Error callback should return None to let other handlers produce the response."""
        cb = CircuitBreaker(failure_threshold=5)
        on_error = cb.on_tool_error_callback()
        result = on_error(self._make_tool("t"), {}, MagicMock(), RuntimeError("x"))
        assert result is None


# ── with_retry decorator ─────────────────────────────────────────────


class TestWithRetry:
    def test_succeeds_first_try(self):
        @with_retry(max_retries=3)
        def good():
            return "ok"

        assert good() == "ok"

    def test_retries_on_transient_error(self):
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.01)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient")
            return "recovered"

        assert flaky() == "recovered"
        assert call_count == 3

    def test_raises_after_max_retries(self):
        @with_retry(max_retries=2, base_delay=0.01)
        def always_fail():
            raise ConnectionError("persistent")

        with pytest.raises(ConnectionError, match="persistent"):
            always_fail()

    def test_no_retry_on_non_retryable(self):
        call_count = 0

        @with_retry(max_retries=3, retryable=(ConnectionError,))
        def bad():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError, match="not retryable"):
            bad()
        assert call_count == 1

    def test_preserves_function_metadata(self):
        @with_retry(max_retries=2)
        def my_tool():
            """My docstring."""
            return 42

        assert my_tool.__name__ == "my_tool"
        assert my_tool.__doc__ == "My docstring."

    def test_custom_retryable_exceptions(self):
        call_count = 0

        @with_retry(max_retries=2, base_delay=0.01, retryable=(ValueError,))
        def custom():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("retry me")
            return "ok"

        assert custom() == "ok"
        assert call_count == 2

    def test_respects_max_delay(self):
        """Backoff should not exceed max_delay."""
        call_count = 0

        @with_retry(max_retries=5, base_delay=100.0, max_delay=0.01)
        def capped():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("x")
            return "ok"

        # Should complete quickly because max_delay caps the sleep
        assert capped() == "ok"

    def test_passes_arguments_through(self):
        @with_retry(max_retries=1)
        def add(a, b, extra=0):
            return a + b + extra

        assert add(1, 2, extra=3) == 6


# ── Async with_retry ────────────────────────────────────────────────


class TestWithRetryAsync:
    def test_async_succeeds_first_try(self):
        @with_retry(max_retries=3)
        async def good():
            return "ok"

        assert asyncio.run(good()) == "ok"

    def test_async_retries_on_transient_error(self):
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.01)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient")
            return "recovered"

        assert asyncio.run(flaky()) == "recovered"
        assert call_count == 3

    def test_async_raises_after_max_retries(self):
        @with_retry(max_retries=2, base_delay=0.01)
        async def always_fail():
            raise ConnectionError("persistent")

        with pytest.raises(ConnectionError, match="persistent"):
            asyncio.run(always_fail())

    def test_async_no_retry_on_non_retryable(self):
        call_count = 0

        @with_retry(max_retries=3, retryable=(ConnectionError,))
        async def bad():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError, match="not retryable"):
            asyncio.run(bad())
        assert call_count == 1

    def test_async_preserves_function_metadata(self):
        @with_retry(max_retries=2)
        async def my_async_tool():
            """My async docstring."""
            return 42

        assert my_async_tool.__name__ == "my_async_tool"
        assert my_async_tool.__doc__ == "My async docstring."

    def test_async_uses_asyncio_sleep(self):
        call_count = 0

        @with_retry(max_retries=2, base_delay=0.01)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("transient")
            return "ok"

        async def run_with_mock():
            with patch("ai_agents_core.resilience.asyncio.sleep") as mock_sleep:
                mock_sleep.return_value = None
                await flaky()
                assert mock_sleep.called

        asyncio.run(run_with_mock())

    def test_async_passes_arguments_through(self):
        @with_retry(max_retries=1)
        async def add(a, b, extra=0):
            return a + b + extra

        assert asyncio.run(add(1, 2, extra=3)) == 6
