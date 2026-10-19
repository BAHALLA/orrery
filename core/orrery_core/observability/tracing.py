"""OpenTelemetry distributed tracing for the agent runtime.

ADK 2.0 already instruments its own execution with OpenTelemetry — it opens
spans for agent invocations, tool calls, and LLM requests under the
``gcp.vertex.agent`` tracer. What's missing in a bare deployment is a configured
``TracerProvider`` with an exporter, so those spans go nowhere.

This module fills that gap with two pieces:

- :func:`configure_tracing` — installs a global ``TracerProvider`` that exports
  to an OTLP collector (Tempo, Jaeger, Cloud Trace, ...) or to the console for
  local dev. Idempotent and env-driven; a no-op unless explicitly enabled.
- :class:`TracingPlugin` — a thin ADK plugin that *enriches* the spans ADK
  already emits with orrery-specific attributes (request id, user role, tool
  status, token usage) and stamps a ``request_id`` for log↔trace correlation.
  It deliberately does **not** create its own agent/tool/LLM spans, which would
  duplicate ADK's.

Requires the ``orrery-core[otel]`` extra (OpenTelemetry SDK + OTLP exporter).
Importing this module without it raises ``ImportError`` with install guidance —
by design, since nothing else in the package depends on it.

Usage::

    from orrery_core import default_plugins
    # enable_tracing reads OTEL_* env vars and prepends TracingPlugin
    plugins = default_plugins(enable_tracing=True)

Environment variables:

- ``OTEL_TRACING_ENABLED``        — master switch (default: ``false``)
- ``OTEL_EXPORTER_OTLP_ENDPOINT`` — OTLP gRPC endpoint, e.g.
  ``http://localhost:4317``. When unset, spans print to the console.
- ``OTEL_SERVICE_NAME``           — resource service.name (default: ``orrery``)
- ``OTEL_TRACES_SAMPLER_ARG``     — head-sampling ratio 0.0–1.0 (default: 1.0)
- ``ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`` — ADK's own switch for copying
  prompts, tool arguments and results into span attributes. ADK defaults it
  **on**; :func:`configure_tracing` defaults it **off** (see
  :data:`CONTENT_IN_SPANS_ENV`). Set it to ``true`` explicitly to opt back in.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SpanExporter,
    )
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )
    from opentelemetry.trace import Span, StatusCode
except ImportError as exc:  # pragma: no cover — covered by the install-extra path
    raise ImportError(
        "orrery_core.tracing requires OpenTelemetry. Install with: uv sync --extra otel"
    ) from exc

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types as genai_types

from ..security.rbac import USER_ROLE_STATE_KEY
from .log import request_id_var
from .metrics import track_llm_tokens

logger = logging.getLogger("orrery.tracing")

#: ADK copies whole LLM requests (prompts, uploaded text attachments) and tool
#: arguments/results into span attributes unless this is falsy, and it defaults
#: on. A trace backend is a second, longer-lived store with its own access
#: model, so the content stays out of it unless an operator opts in. ADK reads
#: the variable per span, so setting it before the first turn is enough.
CONTENT_IN_SPANS_ENV = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"

# Session-state key under which the per-request correlation id is stored, so it
# survives in the persisted session alongside ``user_role`` and ``_auth``.
REQUEST_ID_STATE_KEY = "request_id"

# Process-wide guard so the global TracerProvider is installed exactly once,
# mirroring the metrics server's _server_started/_server_lock idiom.
_configured = False
_config_lock = threading.Lock()


def _env_truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _build_exporter(endpoint: str | None) -> SpanExporter:
    """Return an OTLP exporter when an endpoint is set, else a console exporter."""
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        logger.info("Tracing → OTLP exporter at %s", endpoint)
        return OTLPSpanExporter(endpoint=endpoint)
    logger.info("Tracing → console exporter (no OTEL_EXPORTER_OTLP_ENDPOINT set)")
    return ConsoleSpanExporter()


def configure_tracing(
    *,
    service_name: str | None = None,
    endpoint: str | None = None,
    sample_ratio: float | None = None,
    exporter: SpanExporter | None = None,
) -> bool:
    """Install a global OpenTelemetry ``TracerProvider``. Idempotent.

    Safe to call multiple times — only the first call in the process installs a
    provider. Returns ``True`` if tracing is active (configured now or already),
    ``False`` if disabled via ``OTEL_TRACING_ENABLED``.

    Args:
        service_name: ``service.name`` resource attribute. Defaults to the
            ``OTEL_SERVICE_NAME`` env var, then ``"orrery"``.
        endpoint: OTLP gRPC endpoint. Defaults to ``OTEL_EXPORTER_OTLP_ENDPOINT``.
            When neither is set, spans are exported to the console.
        sample_ratio: Head-sampling ratio in ``[0.0, 1.0]``. Defaults to
            ``OTEL_TRACES_SAMPLER_ARG``, then ``1.0`` (sample everything).
        exporter: Explicit ``SpanExporter`` (mainly for tests). Overrides
            ``endpoint`` when provided.
    """
    global _configured  # noqa: PLW0603

    if not _env_truthy("OTEL_TRACING_ENABLED"):
        logger.debug("Tracing disabled (set OTEL_TRACING_ENABLED=true to enable)")
        return False

    if os.environ.setdefault(CONTENT_IN_SPANS_ENV, "false").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    ):
        logger.warning(
            "%s is on: span attributes will carry prompts, uploaded text attachments "
            "and tool results to the trace backend.",
            CONTENT_IN_SPANS_ENV,
        )

    with _config_lock:
        if _configured:
            return True

        resolved_name = service_name or os.getenv("OTEL_SERVICE_NAME") or "orrery"
        resolved_ratio = (
            sample_ratio
            if sample_ratio is not None
            else float(os.getenv("OTEL_TRACES_SAMPLER_ARG", "1.0"))
        )
        # ParentBased honours an upstream sampling decision (so a sampled HTTP
        # caller keeps its trace intact) and applies the ratio only at the root.
        sampler = ParentBased(
            root=ALWAYS_ON if resolved_ratio >= 1.0 else TraceIdRatioBased(resolved_ratio)
        )

        resource = Resource.create(
            {"service.name": resolved_name, "service.version": _service_version()}
        )
        provider = TracerProvider(resource=resource, sampler=sampler)
        span_exporter = (
            exporter
            if exporter is not None
            else _build_exporter(endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
        )
        provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(provider)

        _configured = True
        logger.info("OpenTelemetry tracing configured (service=%s)", resolved_name)
        return True


def _service_version() -> str:
    try:
        from importlib.metadata import version

        return version("orrery-core")
    except Exception:  # pragma: no cover — version lookup is best-effort
        return "unknown"


def _current_span() -> Span:
    """The active span — ADK's tool/LLM/agent span during a callback."""
    return trace.get_current_span()


def _set_attrs(span: Span, attrs: dict[str, Any]) -> None:
    """Set attributes only on a live, recording span (cheap no-op otherwise)."""
    if not span.is_recording():
        return
    for key, value in attrs.items():
        if value is not None:
            span.set_attribute(key, value)


class TracingPlugin(BasePlugin):
    """Enriches ADK's native OpenTelemetry spans with orrery attributes.

    ADK already opens the spans; this plugin annotates the *current* span at
    each callback boundary rather than creating new ones. It also assigns a
    ``request_id`` per user message and propagates it via a ContextVar so log
    lines and traces share one correlation id.
    """

    def __init__(self) -> None:
        super().__init__(name="tracing")

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: genai_types.Content,
    ) -> genai_types.Content | None:
        request_id = invocation_context.invocation_id or os.urandom(8).hex()
        request_id_var.set(request_id)
        invocation_context.session.state[REQUEST_ID_STATE_KEY] = request_id
        _set_attrs(_current_span(), {"orrery.request_id": request_id})
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        _set_attrs(
            _current_span(),
            {
                "orrery.tool.name": tool.name,
                "orrery.user_role": tool_context.state.get(USER_ROLE_STATE_KEY),
                "orrery.request_id": tool_context.state.get(REQUEST_ID_STATE_KEY),
            },
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        status = result.get("status") if isinstance(result, dict) else None
        _set_attrs(
            _current_span(),
            {
                "orrery.tool.status": status,
                "orrery.tool.result_size": len(str(result)),
            },
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict | None:
        span = _current_span()
        if span.is_recording():
            span.record_exception(error)
            span.set_status(StatusCode.ERROR, str(error))
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        # ADK's native instrumentation already records gen_ai.usage.* on the
        # call_llm span, so we don't duplicate those attributes here — we only
        # bridge the token counts into the orrery_llm_tokens_total Prometheus
        # counter, which ADK does not feed.
        usage = getattr(llm_response, "usage_metadata", None)
        if usage is None:
            return None

        track_llm_tokens(
            callback_context.agent_name,
            usage.prompt_token_count or 0,
            usage.candidates_token_count or 0,
        )
        return None

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse | None:
        span = _current_span()
        if span.is_recording():
            span.record_exception(error)
            span.set_status(StatusCode.ERROR, str(error))
        return None
