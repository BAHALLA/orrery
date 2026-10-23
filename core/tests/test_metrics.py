"""Unit tests for Prometheus metrics collection."""

from unittest.mock import MagicMock, patch

from orrery_core.observability.metrics import (
    CIRCUIT_BREAKER_STATE,
    LLM_TOKENS_TOTAL,
    TOOL_CALLS_TOTAL,
    TOOL_DURATION_SECONDS,
    TOOL_ERRORS_TOTAL,
    MetricsCollector,
    _normalise_status,
    track_llm_tokens,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_tool(name: str = "test_tool") -> MagicMock:
    tool = MagicMock()
    tool.name = name
    return tool


def _make_context(agent_name: str = "test_agent", session_id: str = "sess-1") -> MagicMock:
    ctx = MagicMock()
    ctx.agent_name = agent_name
    ctx.session.id = session_id
    # Use a real dict for state so __setitem__ and .get() work like ADK State
    ctx.state = {}
    return ctx


def _sample_value(metric, labels: dict) -> float:
    """Get the current value of a metric with the given labels."""
    return metric.labels(**labels)._value.get()


# ── _normalise_status ─────────────────────────────────────────────────


class TestNormaliseStatus:
    def test_allowed_values_pass_through(self):
        for s in ("ok", "success", "error", "confirmation_required"):
            assert _normalise_status(s) == s

    def test_unknown_values_map_to_ok(self):
        assert _normalise_status("some_random_status") == "ok"
        assert _normalise_status("") == "ok"


# ── MetricsCollector before_tool_callback ─────────────────────────────


class TestBeforeToolCallback:
    def test_stores_timer(self):
        mc = MetricsCollector()
        cb = mc.before_tool_callback()
        ctx = _make_context()

        cb(tool=_make_tool(), args={}, tool_context=ctx)

        assert len(mc._timers) == 1
        assert "_metrics_invocation_id" in ctx.state

    def test_does_not_block_tool(self):
        mc = MetricsCollector()
        cb = mc.before_tool_callback()
        result = cb(tool=_make_tool(), args={}, tool_context=_make_context())
        assert result is None

    def test_concurrent_calls_get_unique_ids(self):
        mc = MetricsCollector()
        cb = mc.before_tool_callback()
        ctx1 = _make_context()
        ctx2 = _make_context()

        cb(tool=_make_tool("t"), args={}, tool_context=ctx1)
        cb(tool=_make_tool("t"), args={}, tool_context=ctx2)

        assert len(mc._timers) == 2
        assert ctx1.state["_metrics_invocation_id"] != ctx2.state["_metrics_invocation_id"]


# ── MetricsCollector after_tool_callback ──────────────────────────────


class TestAfterToolCallback:
    def test_records_success_count(self):
        mc = MetricsCollector()
        cb = mc.after_tool_callback()
        tool = _make_tool("my_tool")
        ctx = _make_context("ag")

        before = _sample_value(TOOL_CALLS_TOTAL, {"agent": "ag", "tool": "my_tool", "status": "ok"})
        cb(tool=tool, args={}, tool_context=ctx, tool_response={"status": "ok"})
        after = _sample_value(TOOL_CALLS_TOTAL, {"agent": "ag", "tool": "my_tool", "status": "ok"})

        assert after - before == 1.0

    def test_normalises_unknown_status(self):
        mc = MetricsCollector()
        cb = mc.after_tool_callback()
        tool = _make_tool("norm_tool")
        ctx = _make_context("norm_ag")

        before = _sample_value(
            TOOL_CALLS_TOTAL, {"agent": "norm_ag", "tool": "norm_tool", "status": "ok"}
        )
        cb(tool=tool, args={}, tool_context=ctx, tool_response={"status": "weird_value"})
        after = _sample_value(
            TOOL_CALLS_TOTAL, {"agent": "norm_ag", "tool": "norm_tool", "status": "ok"}
        )

        assert after - before == 1.0

    def test_records_error_status_from_response(self):
        mc = MetricsCollector()
        cb = mc.after_tool_callback()
        tool = _make_tool("err_tool")
        ctx = _make_context("ag2")

        before = _sample_value(
            TOOL_CALLS_TOTAL, {"agent": "ag2", "tool": "err_tool", "status": "error"}
        )
        cb(tool=tool, args={}, tool_context=ctx, tool_response={"status": "error"})
        after = _sample_value(
            TOOL_CALLS_TOTAL, {"agent": "ag2", "tool": "err_tool", "status": "error"}
        )

        assert after - before == 1.0

    def test_records_duration(self):
        mc = MetricsCollector()
        before_cb = mc.before_tool_callback()
        after_cb = mc.after_tool_callback()
        tool = _make_tool("dur_tool")
        ctx = _make_context("dur_agent")

        before_cb(tool=tool, args={}, tool_context=ctx)
        after_cb(tool=tool, args={}, tool_context=ctx, tool_response={})

        hist = TOOL_DURATION_SECONDS.labels(agent="dur_agent", tool="dur_tool")
        assert hist._sum.get() > 0

    def test_clears_timer(self):
        mc = MetricsCollector()
        before_cb = mc.before_tool_callback()
        after_cb = mc.after_tool_callback()
        tool = _make_tool()
        ctx = _make_context()

        before_cb(tool=tool, args={}, tool_context=ctx)
        assert len(mc._timers) == 1

        after_cb(tool=tool, args={}, tool_context=ctx, tool_response={})
        assert len(mc._timers) == 0

    def test_handles_missing_timer(self):
        mc = MetricsCollector()
        cb = mc.after_tool_callback()
        result = cb(tool=_make_tool(), args={}, tool_context=_make_context(), tool_response={})
        assert result is None

    def test_returns_none(self):
        mc = MetricsCollector()
        cb = mc.after_tool_callback()
        result = cb(tool=_make_tool(), args={}, tool_context=_make_context(), tool_response={})
        assert result is None


# ── MetricsCollector on_tool_error_callback ───────────────────────────


class TestOnToolErrorCallback:
    def test_records_error_count(self):
        mc = MetricsCollector()
        cb = mc.on_tool_error_callback()
        tool = _make_tool("fail_tool")
        ctx = _make_context("err_ag")

        before = _sample_value(
            TOOL_ERRORS_TOTAL,
            {"agent": "err_ag", "tool": "fail_tool", "error_type": "ValueError"},
        )
        cb(tool=tool, args={}, tool_context=ctx, error=ValueError("boom"))
        after = _sample_value(
            TOOL_ERRORS_TOTAL,
            {"agent": "err_ag", "tool": "fail_tool", "error_type": "ValueError"},
        )

        assert after - before == 1.0

    def test_records_error_in_calls_total(self):
        mc = MetricsCollector()
        cb = mc.on_tool_error_callback()
        tool = _make_tool("fail2")
        ctx = _make_context("ea")

        before = _sample_value(
            TOOL_CALLS_TOTAL, {"agent": "ea", "tool": "fail2", "status": "error"}
        )
        cb(tool=tool, args={}, tool_context=ctx, error=RuntimeError("x"))
        after = _sample_value(TOOL_CALLS_TOTAL, {"agent": "ea", "tool": "fail2", "status": "error"})

        assert after - before == 1.0

    def test_records_duration_on_error(self):
        mc = MetricsCollector()
        before_cb = mc.before_tool_callback()
        error_cb = mc.on_tool_error_callback()
        tool = _make_tool("errdur")
        ctx = _make_context("eda")

        before_cb(tool=tool, args={}, tool_context=ctx)
        error_cb(tool=tool, args={}, tool_context=ctx, error=OSError("fail"))

        hist = TOOL_DURATION_SECONDS.labels(agent="eda", tool="errdur")
        assert hist._sum.get() > 0

    def test_returns_none(self):
        mc = MetricsCollector()
        cb = mc.on_tool_error_callback()
        result = cb(tool=_make_tool(), args={}, tool_context=_make_context(), error=Exception("e"))
        assert result is None


# ── Circuit breaker integration ───────────────────────────────────────


class TestCircuitBreakerGauge:
    def test_updates_gauge_on_before_callback(self):
        from orrery_core.reliability.resilience import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=2)
        mc = MetricsCollector(circuit_breaker=breaker)
        cb = mc.before_tool_callback()

        cb(tool=_make_tool("cb_tool"), args={}, tool_context=_make_context())
        assert CIRCUIT_BREAKER_STATE.labels(tool="cb_tool")._value.get() == 0  # closed

    def test_reflects_open_state(self):
        from orrery_core.reliability.resilience import CircuitBreaker

        breaker = CircuitBreaker(failure_threshold=2)
        breaker._record_failure("open_tool")
        breaker._record_failure("open_tool")  # opens circuit

        mc = MetricsCollector(circuit_breaker=breaker)
        cb = mc.before_tool_callback()
        cb(tool=_make_tool("open_tool"), args={}, tool_context=_make_context())

        assert CIRCUIT_BREAKER_STATE.labels(tool="open_tool")._value.get() == 1  # open

    def test_no_breaker_is_noop(self):
        mc = MetricsCollector()  # no circuit_breaker
        cb = mc.before_tool_callback()
        # Should not raise
        cb(tool=_make_tool("noop_tool"), args={}, tool_context=_make_context())


# ── track_llm_tokens ─────────────────────────────────────────────────


class TestTrackLlmTokens:
    def test_increments_input_tokens(self):
        before = _sample_value(LLM_TOKENS_TOTAL, {"agent": "tok_ag", "direction": "input"})
        track_llm_tokens("tok_ag", input_tokens=100, output_tokens=0)
        after = _sample_value(LLM_TOKENS_TOTAL, {"agent": "tok_ag", "direction": "input"})
        assert after - before == 100

    def test_increments_output_tokens(self):
        before = _sample_value(LLM_TOKENS_TOTAL, {"agent": "tok_ag2", "direction": "output"})
        track_llm_tokens("tok_ag2", input_tokens=0, output_tokens=50)
        after = _sample_value(LLM_TOKENS_TOTAL, {"agent": "tok_ag2", "direction": "output"})
        assert after - before == 50


# ── start_server ──────────────────────────────────────────────────────


class TestStartServer:
    @patch("orrery_core.observability.metrics.start_http_server")
    def test_starts_server_default_port(self, mock_start):
        import orrery_core.observability.metrics as m

        m._server_started = False
        mc = MetricsCollector()
        mc.start_server(port=9200)
        mock_start.assert_called_once_with(9200)
        m._server_started = False  # reset for other tests

    @patch("orrery_core.observability.metrics.start_http_server")
    def test_idempotent_across_instances(self, mock_start):
        import orrery_core.observability.metrics as m

        m._server_started = False
        mc1 = MetricsCollector()
        mc2 = MetricsCollector()
        mc1.start_server(port=9201)
        mc2.start_server(port=9201)
        mock_start.assert_called_once()
        m._server_started = False

    @patch.dict("os.environ", {"METRICS_PORT": "9300"})
    @patch("orrery_core.observability.metrics.start_http_server")
    def test_reads_port_from_env(self, mock_start):
        import orrery_core.observability.metrics as m

        m._server_started = False
        mc = MetricsCollector()
        mc.start_server()  # no explicit port
        mock_start.assert_called_once_with(9300)
        m._server_started = False


class TestStartMetricsServerIfEnabled:
    @patch.dict("os.environ", {"ENABLE_METRICS_SERVER": "false"})
    @patch("orrery_core.observability.metrics.start_http_server")
    def test_off_unless_enabled(self, mock_start):
        from orrery_core.observability.metrics import start_metrics_server_if_enabled

        assert start_metrics_server_if_enabled() is False
        mock_start.assert_not_called()

    @patch.dict("os.environ", {"ENABLE_METRICS_SERVER": "true", "METRICS_PORT": "9301"})
    @patch("orrery_core.observability.metrics.start_http_server")
    def test_starts_on_metrics_port(self, mock_start):
        import orrery_core.observability.metrics as m

        m._server_started = False
        assert m.start_metrics_server_if_enabled() is True
        mock_start.assert_called_once_with(9301)
        m._server_started = False

    @patch.dict("os.environ", {"ENABLE_METRICS_SERVER": "true"})
    @patch(
        "orrery_core.observability.metrics.start_http_server",
        side_effect=OSError("Address already in use"),
    )
    def test_bind_failure_is_logged_not_raised(self, _mock_start, caplog):
        import orrery_core.observability.metrics as m

        m._server_started = False
        assert m.start_metrics_server_if_enabled() is False
        assert "Could not start the Prometheus metrics server" in caplog.text
        assert m._server_started is False
