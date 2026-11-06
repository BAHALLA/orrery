"""A bound on the inbound ``tracestate`` header, active whether or not tracing is on.

FastAPI's native telemetry extracts trace headers once any tracer provider
exists, and that provider may not be ours. So the bound does not wait for
:func:`~orrery_core.observability.tracing.configure_tracing`: ``default_plugins()``
installs it on every transport. This module needs only ``opentelemetry-api``,
which ADK always brings, not the ``[otel]`` extra.
"""

from __future__ import annotations

from opentelemetry import propagate
from opentelemetry.context import Context
from opentelemetry.propagators import textmap

#: Longest ``tracestate`` (all header lines together) handed to OpenTelemetry's
#: parser. ``TraceState.from_header`` splits with a regex that is quadratic in
#: a run of blanks (GHSA for opentelemetry-api <= 1.45.0): ~100 KB costs ~6 s of
#: CPU on the event loop, and FastAPI's native telemetry extracts the inbound
#: headers of every request, before auth, once a tracer provider exists. W3C
#: Trace Context asks vendors to propagate at least 512 characters; a longer
#: one is dropped whole, which keeps the trace (``traceparent``) and loses only
#: vendor state.
MAX_TRACESTATE_CHARS = 512


class _BoundedTraceStateGetter(textmap.Getter[textmap.CarrierT]):
    """Hides an oversized ``tracestate`` from the propagators behind it."""

    def __init__(self, inner: textmap.Getter[textmap.CarrierT]) -> None:
        self._inner = inner

    def get(self, carrier: textmap.CarrierT, key: str) -> list[str] | None:
        values = self._inner.get(carrier, key)
        if (
            values
            and key.lower() == "tracestate"
            and sum(len(value) for value in values) > MAX_TRACESTATE_CHARS
        ):
            return None
        return values

    def keys(self, carrier: textmap.CarrierT) -> list[str]:
        return self._inner.keys(carrier)


class BoundedTraceStatePropagator(textmap.TextMapPropagator):
    """Wraps a propagator so extraction never parses an oversized ``tracestate``."""

    def __init__(self, inner: textmap.TextMapPropagator) -> None:
        self.inner = inner

    def extract(
        self,
        carrier: textmap.CarrierT,
        context: Context | None = None,
        getter: textmap.Getter[textmap.CarrierT] = textmap.default_getter,
    ) -> Context:
        return self.inner.extract(carrier, context, _BoundedTraceStateGetter(getter))

    def inject(
        self,
        carrier: textmap.CarrierT,
        context: Context | None = None,
        setter: textmap.Setter[textmap.CarrierT] = textmap.default_setter,
    ) -> None:
        self.inner.inject(carrier, context, setter)

    @property
    def fields(self) -> set[str]:
        return self.inner.fields


def bound_inbound_tracestate() -> None:
    """Install :class:`BoundedTraceStatePropagator` around the global propagator."""
    current = propagate.get_global_textmap()
    if not isinstance(current, BoundedTraceStatePropagator):
        propagate.set_global_textmap(BoundedTraceStatePropagator(current))
