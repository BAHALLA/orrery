"""Tests for OpenTelemetry tracing (configure_tracing + TracingPlugin).

Span enrichment is verified with an in-memory exporter and a locally-built
``TracerProvider`` so the assertions are deterministic and don't depend on the
process-global provider installed by ``configure_tracing``.
"""

from __future__ import annotations

import contextlib
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from orrery_core.observability import tracing
from orrery_core.observability.log import JSONFormatter, request_id_var
from orrery_core.observability.tracing import REQUEST_ID_STATE_KEY, TracingPlugin, configure_tracing

from .conftest import FakeState, FakeTool


@pytest.fixture(autouse=True)
def _reset_tracing_guard(monkeypatch):
    """Make each tracing test hermetic.

    A developer's ``.env`` (loaded via ``load_dotenv`` during collection) may
    leave ``OTEL_TRACING_ENABLED=true`` in ``os.environ``, and OpenTelemetry's
    global ``TracerProvider`` can only be set once per process. Reset both so
    tests are deterministic regardless of suite order or the ambient env.
    """
    monkeypatch.delenv("OTEL_TRACING_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    tracing._configured = False
    request_id_var.set(None)
    # Allow configure_tracing() to install a fresh global provider.
    trace._TRACER_PROVIDER_SET_ONCE = trace.Once()
    trace._TRACER_PROVIDER = None
    yield
    tracing._configured = False
    request_id_var.set(None)
    trace._TRACER_PROVIDER_SET_ONCE = trace.Once()
    trace._TRACER_PROVIDER = None


@contextlib.contextmanager
def recording_span(name: str = "root"):
    """Make a recording span current and collect it after the block exits."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(name):
        yield exporter


def only_span(exporter: InMemorySpanExporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected one span, got {len(spans)}"
    return spans[0]


# Duck-typed stand-ins for ADK's strongly-typed callback arguments. Returning
# ``Any`` keeps the type checker from rejecting these fakes where ADK declares
# concrete Context / ToolContext / LlmResponse / BaseTool parameters.
def _fake(**attrs: Any) -> Any:
    return SimpleNamespace(**attrs)


def _tool(name: str) -> Any:
    return FakeTool(name)


# ── configure_tracing ────────────────────────────────────────────────


def test_configure_tracing_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_TRACING_ENABLED", raising=False)
    assert configure_tracing(exporter=InMemorySpanExporter()) is False
    assert tracing._configured is False


def test_configure_tracing_enabled_is_idempotent(monkeypatch):
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "orrery-test")
    exporter = InMemorySpanExporter()

    assert configure_tracing(exporter=exporter) is True
    assert tracing._configured is True
    # Second call short-circuits via the guard and still reports active.
    assert configure_tracing(exporter=exporter) is True

    provider: Any = trace.get_tracer_provider()
    assert provider.resource.attributes["service.name"] == "orrery-test"


def _traced_llm_request(text: str) -> str:
    """What ADK writes into a real LLM span for a request carrying *text*."""
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.telemetry.tracing import trace_call_llm
    from google.genai import types

    request = LlmRequest(
        model="m", contents=[types.Content(role="user", parts=[types.Part(text=text)])]
    )
    ctx = _fake(invocation_id="inv", session=_fake(id="s"), run_config=None)
    with recording_span() as exporter:
        trace_call_llm(ctx, "e1", request, LlmResponse())
    return json.dumps(dict(only_span(exporter).attributes or {}))


def test_configure_tracing_keeps_message_content_out_of_spans(monkeypatch):
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "true")
    monkeypatch.setenv(tracing.CONTENT_IN_SPANS_ENV, "x")
    monkeypatch.delenv(tracing.CONTENT_IN_SPANS_ENV)

    assert configure_tracing(exporter=InMemorySpanExporter()) is True

    # An uploaded log, a prompt, a tool result: none of it reaches the backend.
    assert "SECRET-LOG-LINE" not in _traced_llm_request("SECRET-LOG-LINE")


def test_an_explicit_span_content_opt_in_is_honoured(monkeypatch, caplog):
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "true")
    monkeypatch.setenv(tracing.CONTENT_IN_SPANS_ENV, "true")

    assert configure_tracing(exporter=InMemorySpanExporter()) is True

    assert "SECRET-LOG-LINE" in _traced_llm_request("SECRET-LOG-LINE")
    assert "span attributes will carry prompts" in caplog.text


# ── TracingPlugin: request id / user message ─────────────────────────


@pytest.mark.asyncio
async def test_on_user_message_assigns_request_id():
    plugin = TracingPlugin()
    session = _fake(state=FakeState())
    inv_ctx = _fake(invocation_id="inv-42", session=session)

    with recording_span() as exporter:
        await plugin.on_user_message_callback(invocation_context=inv_ctx, user_message=_fake())

    assert request_id_var.get() == "inv-42"
    assert session.state[REQUEST_ID_STATE_KEY] == "inv-42"
    assert only_span(exporter).attributes["orrery.request_id"] == "inv-42"


# ── TracingPlugin: tool enrichment ───────────────────────────────────


@pytest.mark.asyncio
async def test_before_tool_sets_role_and_request_id():
    plugin = TracingPlugin()
    ctx = _fake(state=FakeState({"user_role": "operator", REQUEST_ID_STATE_KEY: "req-7"}))

    with recording_span() as exporter:
        await plugin.before_tool_callback(
            tool=_tool("restart_broker"), tool_args={}, tool_context=ctx
        )

    attrs = only_span(exporter).attributes
    assert attrs["orrery.tool.name"] == "restart_broker"
    assert attrs["orrery.user_role"] == "operator"
    assert attrs["orrery.request_id"] == "req-7"


@pytest.mark.asyncio
async def test_after_tool_records_status_and_size():
    plugin = TracingPlugin()
    result = {"status": "success", "data": "ok"}

    with recording_span() as exporter:
        await plugin.after_tool_callback(
            tool=_tool("list_topics"),
            tool_args={},
            tool_context=_fake(state=FakeState()),
            result=result,
        )

    attrs = only_span(exporter).attributes
    assert attrs["orrery.tool.status"] == "success"
    assert attrs["orrery.tool.result_size"] == len(str(result))


@pytest.mark.asyncio
async def test_on_tool_error_marks_span_error():
    plugin = TracingPlugin()

    with recording_span() as exporter:
        await plugin.on_tool_error_callback(
            tool=_tool("scale_deployment"),
            tool_args={},
            tool_context=_fake(state=FakeState()),
            error=ValueError("boom"),
        )

    span = only_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


# ── TracingPlugin: model / token usage ───────────────────────────────


@pytest.mark.asyncio
async def test_after_model_forwards_tokens_to_metrics(monkeypatch):
    # ADK natively records gen_ai.usage.* on the span; the plugin's only job is
    # to bridge the token counts into the Prometheus counter.
    plugin = TracingPlugin()
    recorded: list[tuple] = []
    monkeypatch.setattr(tracing, "track_llm_tokens", lambda *a: recorded.append(a))

    usage = _fake(prompt_token_count=120, candidates_token_count=45, total_token_count=165)
    llm_response = _fake(usage_metadata=usage)
    cb_ctx = _fake(agent_name="kafka")

    await plugin.after_model_callback(callback_context=cb_ctx, llm_response=llm_response)

    assert recorded == [("kafka", 120, 45)]


@pytest.mark.asyncio
async def test_after_model_without_usage_is_noop(monkeypatch):
    plugin = TracingPlugin()
    monkeypatch.setattr(
        tracing,
        "track_llm_tokens",
        lambda *a: pytest.fail("should not record tokens without usage"),
    )
    out = await plugin.after_model_callback(
        callback_context=_fake(agent_name="x"),
        llm_response=_fake(usage_metadata=None),
    )
    assert out is None


# ── log ↔ trace correlation ──────────────────────────────────────────


def _format(record_kwargs: dict | None = None) -> dict:
    record = logging.LogRecord(
        name="orrery.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    return json.loads(JSONFormatter().format(record))


def test_log_includes_request_id_when_set():
    request_id_var.set("req-abc")
    entry = _format()
    assert entry["request_id"] == "req-abc"


def test_log_omits_correlation_when_absent():
    entry = _format()
    assert "request_id" not in entry
    assert "trace_id" not in entry


def test_log_includes_trace_id_within_span():
    with recording_span():
        entry = _format()
    assert len(entry["trace_id"]) == 32
    assert len(entry["span_id"]) == 16
