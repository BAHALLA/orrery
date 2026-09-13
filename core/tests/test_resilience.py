"""Unit tests for resilience utilities (circuit breaker + retry)."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from orrery_core.reliability.resilience import (
    CircuitBreaker,
    CircuitState,
    ToolOutcome,
    classify_tool_outcome,
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
        with patch("orrery_core.reliability.resilience.time") as mock_time:
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
            with patch("orrery_core.reliability.resilience.asyncio.sleep") as mock_sleep:
                mock_sleep.return_value = None
                await flaky()
                assert mock_sleep.called

        asyncio.run(run_with_mock())

    def test_async_passes_arguments_through(self):
        @with_retry(max_retries=1)
        async def add(a, b, extra=0):
            return a + b + extra

        assert asyncio.run(add(1, 2, extra=3)) == 6


# ── Outcome classification ───────────────────────────────────────────


class TestClassifyToolOutcome:
    """Tools here catch their own exceptions and return an error *dict*, so the
    classifier — not ``on_tool_error_callback`` — is what sees a real outage."""

    def test_error_status_is_a_failure(self):
        response = {"status": "error", "message": "Failed to connect to Kafka: timed out"}
        assert classify_tool_outcome(response) is ToolOutcome.FAILURE

    @pytest.mark.parametrize("status", ["success", "ok", "recorded", "dry_run", "truncated"])
    def test_non_error_statuses_are_successes(self, status):
        assert classify_tool_outcome({"status": status}) is ToolOutcome.SUCCESS

    def test_missing_status_is_a_success(self):
        assert classify_tool_outcome({"topics": ["a", "b"], "count": 2}) is ToolOutcome.SUCCESS

    def test_subsystem_health_verdict_is_not_a_tool_failure(self):
        """A checker reporting an unhealthy subsystem still *worked*."""
        assert classify_tool_outcome({"status": "fail", "summary": "pod crashlooping"}) is (
            ToolOutcome.SUCCESS
        )

    @pytest.mark.parametrize(
        "status", ["BLOCKED", "AWAITING_CONFIRMATION", "access_denied", "confirmation_required"]
    )
    def test_gate_responses_are_ignored(self, status):
        """A gate answered instead of the tool — that is evidence of neither."""
        assert classify_tool_outcome({"status": status}) is ToolOutcome.IGNORE

    @pytest.mark.parametrize("error_type", ["CircuitOpen", "CircuitHalfOpen"])
    def test_breakers_own_refusals_are_ignored(self, error_type):
        """Counting these would re-stamp ``_opened_at`` and wedge the circuit open."""
        response = {"status": "error", "error_type": error_type, "message": "unavailable"}
        assert classify_tool_outcome(response) is ToolOutcome.IGNORE

    def test_non_dict_results_are_handled(self):
        """ADK returns a tool's value verbatim — it need not be a dict."""
        assert classify_tool_outcome("a bare string") is ToolOutcome.SUCCESS
        assert classify_tool_outcome(None) is ToolOutcome.SUCCESS
        assert classify_tool_outcome([1, 2, 3]) is ToolOutcome.SUCCESS

    def test_pydantic_result_is_read_by_attribute(self):
        from orrery_core.tools.tool_result import ToolResult

        assert classify_tool_outcome(ToolResult.ok(topics=[])) is ToolOutcome.SUCCESS
        assert classify_tool_outcome(ToolResult.error("boom")) is ToolOutcome.FAILURE

    def test_non_string_status_is_not_mistaken_for_one(self):
        assert classify_tool_outcome({"status": 500}) is ToolOutcome.SUCCESS


class TestNeutralStatusesStayInSync:
    """``_NEUTRAL_STATUSES`` duplicates literals owned by other modules (a
    ``plugins`` import here would be circular). Pin them to their real source so
    a rename there fails the build instead of silently un-ignoring a gate."""

    def test_autonomy_deny_and_pause_are_neutral(self):
        from orrery_core.plugins.autonomy_plugin import _awaiting_confirmation, _deny

        assert classify_tool_outcome(_deny("scale_deployment", "L2", "read-only")) is (
            ToolOutcome.IGNORE
        )
        assert classify_tool_outcome(_awaiting_confirmation("scale_deployment", "L4")) is (
            ToolOutcome.IGNORE
        )

    def test_guardrails_confirmation_prompt_is_neutral(self):
        from orrery_core.security.guardrails import _confirmation_prompt

        tool = MagicMock()
        tool.name = "delete_topic"
        payload = _confirmation_prompt(
            tool=tool, func=lambda: None, args={}, level="destructive", strict=True
        )
        assert classify_tool_outcome(payload) is ToolOutcome.IGNORE

    def test_rbac_denial_status_is_neutral(self):
        from orrery_core.security.rbac import Role, RolePolicy, authorize

        tool = MagicMock()
        tool.name = "delete_kafka_topic"
        tool.func = None
        ctx = MagicMock()
        ctx.state = {"user_role": "viewer"}
        callback = authorize(policy=RolePolicy(overrides={"delete_kafka_topic": Role.ADMIN}))
        denial = callback(tool=tool, args={}, tool_context=ctx)
        assert denial is not None, "expected RBAC to deny a viewer an admin tool"
        assert classify_tool_outcome(denial) is ToolOutcome.IGNORE


# ── Regression: failures that never raise ────────────────────────────


class TestGracefullyHandledFailuresOpenTheCircuit:
    """The failure mode the breaker exists for, driven end to end.

    Every tool in this repo wraps its client in ``try/except`` and returns
    ``{"status": "error", ...}``. ``on_tool_error_callback`` therefore never
    fires for a broker outage, so a breaker that only counts raised exceptions
    can never open. Unit-testing ``_record_failure`` directly cannot catch that
    — these drive the ADK callbacks the Runner actually calls.
    """

    def _make_tool(self, name: str) -> MagicMock:
        tool = MagicMock()
        tool.name = name
        return tool

    def test_repeated_error_dicts_open_the_circuit(self):
        cb = CircuitBreaker(failure_threshold=3)
        after = cb.after_tool_callback()
        outage = {"status": "error", "message": "Failed to connect to Kafka: timed out"}

        for _ in range(3):
            assert after(self._make_tool("list_kafka_topics"), {}, MagicMock(), outage) is None

        assert cb.state("list_kafka_topics") == CircuitState.OPEN

    def test_a_success_between_errors_resets_the_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        after = cb.after_tool_callback()
        tool = self._make_tool("list_kafka_topics")

        after(tool, {}, MagicMock(), {"status": "error"})
        after(tool, {}, MagicMock(), {"status": "error"})
        after(tool, {}, MagicMock(), {"status": "success", "topics": []})
        after(tool, {}, MagicMock(), {"status": "error"})

        assert cb.state("list_kafka_topics") == CircuitState.CLOSED

    def test_gate_denials_move_the_circuit_in_neither_direction(self):
        cb = CircuitBreaker(failure_threshold=3)
        after = cb.after_tool_callback()
        tool = self._make_tool("scale_deployment")

        after(tool, {}, MagicMock(), {"status": "error"})
        after(tool, {}, MagicMock(), {"status": "error"})
        # A blocked call is not evidence the tool recovered…
        after(tool, {}, MagicMock(), {"status": "BLOCKED", "message": "L2 is read-only"})
        after(tool, {}, MagicMock(), {"status": "error"})
        assert cb.state("scale_deployment") == CircuitState.OPEN

    def test_blocked_calls_alone_never_open_the_circuit(self):
        cb = CircuitBreaker(failure_threshold=2)
        after = cb.after_tool_callback()
        tool = self._make_tool("scale_deployment")

        for _ in range(5):
            after(tool, {}, MagicMock(), {"status": "access_denied", "tool": "scale_deployment"})

        assert cb.state("scale_deployment") == CircuitState.CLOSED

    def test_open_circuit_refusals_do_not_extend_the_open_window(self):
        """The refusal the breaker itself returns must not re-arm the timer."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10.0)
        before, after = cb.before_tool_callback(), cb.after_tool_callback()
        tool = self._make_tool("list_kafka_topics")

        after(tool, {}, MagicMock(), {"status": "error"})
        after(tool, {}, MagicMock(), {"status": "error"})
        opened_at = cb._opened_at["list_kafka_topics"]

        refusal = before(tool, {}, MagicMock())
        assert refusal["error_type"] == "CircuitOpen"
        after(tool, {}, MagicMock(), refusal)

        assert cb._opened_at["list_kafka_topics"] == opened_at

    def test_failed_probe_reopens_the_circuit(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.0)
        before, after = cb.before_tool_callback(), cb.after_tool_callback()
        tool = self._make_tool("list_kafka_topics")

        after(tool, {}, MagicMock(), {"status": "error"})
        after(tool, {}, MagicMock(), {"status": "error"})
        assert before(tool, {}, MagicMock()) is None  # probe allowed

        after(tool, {}, MagicMock(), {"status": "error", "message": "still down"})

        # Half-open again only because recovery_timeout=0; the point is that the
        # failed probe did not close the circuit.
        assert cb.state("list_kafka_topics") != CircuitState.CLOSED

    def test_successful_probe_closes_the_circuit(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.0)
        before, after = cb.before_tool_callback(), cb.after_tool_callback()
        tool = self._make_tool("list_kafka_topics")

        after(tool, {}, MagicMock(), {"status": "error"})
        after(tool, {}, MagicMock(), {"status": "error"})
        before(tool, {}, MagicMock())
        after(tool, {}, MagicMock(), {"status": "success", "topics": []})

        assert cb.state("list_kafka_topics") == CircuitState.CLOSED

    def test_gate_answering_a_probe_releases_the_probe_slot(self):
        """``require_confirmation()`` is an agent callback, so it can answer
        *after* this plugin marked a probe in flight. Without a release the tool
        stays refused as ``CircuitHalfOpen`` forever."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.0)
        before, after = cb.before_tool_callback(), cb.after_tool_callback()
        tool = self._make_tool("scale_deployment")

        after(tool, {}, MagicMock(), {"status": "error"})
        after(tool, {}, MagicMock(), {"status": "error"})
        assert before(tool, {}, MagicMock()) is None  # probe slot taken
        after(tool, {}, MagicMock(), {"status": "confirmation_required", "message": "approve?"})

        # The next call gets the probe slot rather than a CircuitHalfOpen refusal.
        assert before(tool, {}, MagicMock()) is None


class TestResiliencePluginWiring:
    """The breaker as the Runner actually registers it — via ``ResiliencePlugin``.

    ``default_plugins()`` wires the plugin, not the raw callbacks; a fix applied
    to ``CircuitBreaker`` but not reachable through the plugin is not a fix.
    """

    def _make_tool(self, name: str) -> MagicMock:
        tool = MagicMock()
        tool.name = name
        return tool

    def test_error_dicts_through_the_plugin_open_the_circuit(self):
        from orrery_core.plugins import ResiliencePlugin

        plugin = ResiliencePlugin(failure_threshold=3)
        tool = self._make_tool("list_kafka_topics")
        outage = {"status": "error", "message": "Failed to connect to Kafka: timed out"}

        async def drive():
            for _ in range(3):
                await plugin.after_tool_callback(
                    tool=tool, tool_args={}, tool_context=MagicMock(), result=outage
                )
            # The next call is refused before it reaches the tool.
            return await plugin.before_tool_callback(
                tool=tool, tool_args={}, tool_context=MagicMock()
            )

        refusal = asyncio.run(drive())
        assert plugin.circuit_breaker.state("list_kafka_topics") == CircuitState.OPEN
        assert refusal is not None and refusal["error_type"] == "CircuitOpen"

    def test_plugin_never_replaces_the_tool_response(self):
        from orrery_core.plugins import ResiliencePlugin

        plugin = ResiliencePlugin(failure_threshold=2)

        async def drive():
            return await plugin.after_tool_callback(
                tool=self._make_tool("t"),
                tool_args={},
                tool_context=MagicMock(),
                result={"status": "error"},
            )

        # Returning non-None would early-exit ADK's after-tool chain.
        assert asyncio.run(drive()) is None
