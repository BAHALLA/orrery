"""Prometheus metrics for agent and tool observability.

Provides a ``MetricsCollector`` that integrates with the ADK callback model
to automatically track tool execution counts, durations, errors, and circuit
breaker state.  Also exposes an HTTP ``/metrics`` endpoint for Prometheus
scraping.

Usage::

    from orrery_core.metrics import MetricsCollector

    metrics = MetricsCollector()

    create_agent(
        ...,
        before_tool_callback=[metrics.before_tool_callback()],
        after_tool_callback=[metrics.after_tool_callback()],
        on_tool_error_callback=metrics.on_tool_error_callback(),
    )

    # Start the /metrics HTTP server (call once at startup)
    metrics.start_server(port=9100)
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

logger = logging.getLogger("orrery.metrics")

# ── Metric definitions ────────────────────────────────────────────────
# Naming follows https://prometheus.io/docs/practices/naming/
#   namespace: orrery
#   subsystem: tool / circuit_breaker / llm

TOOL_CALLS_TOTAL = Counter(
    "orrery_tool_calls_total",
    "Total number of tool invocations",
    ["agent", "tool", "status"],
)

TOOL_DURATION_SECONDS = Histogram(
    "orrery_tool_duration_seconds",
    "Tool execution duration in seconds",
    ["agent", "tool"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

TOOL_ERRORS_TOTAL = Counter(
    "orrery_tool_errors_total",
    "Tool errors broken down by exception type",
    ["agent", "tool", "error_type"],
)

CIRCUIT_BREAKER_STATE = Gauge(
    "orrery_circuit_breaker_state",
    "Circuit breaker state: 0=closed, 1=open, 2=half_open",
    ["tool"],
)

LLM_TOKENS_TOTAL = Counter(
    "orrery_llm_tokens_total",
    "Total LLM tokens consumed",
    ["agent", "direction"],
)

LLM_FAILOVER_TOTAL = Counter(
    "orrery_llm_failover_total",
    "Model calls moved to the next model in the fallback chain",
    ["from_model", "to_model"],
)

CONTEXT_CACHE_EVENTS_TOTAL = Counter(
    "orrery_context_cache_events_total",
    "Context cache hit/miss events",
    ["event"],  # "hit" or "miss"
)

CONTEXT_COMPACTION_TOTAL = Counter(
    "orrery_context_compaction_total",
    "Conversation history compactions performed",
)

#: Prompt-injection screening outcomes.
#:
#: ``direction`` separates the two halves of the defence, which have very
#: different meanings and must not be summed together:
#:
#: - ``direct`` — an injected *user message*, blocked in ``before_run_callback``
#:   before it reached a tool or cost a token. Someone is probing the agent.
#: - ``indirect`` — instruction-like text inside a *tool result*, neutralized in
#:   place. Attacker-reachable content is sitting in the monitored
#:   infrastructure (a pod annotation, a log line, an indexed document), which
#:   is a finding about that system rather than about Orrery.
#:
#: ``source`` is the tool name for indirect hits and ``"user_message"`` for
#: direct ones, so an alert can point at where the text came from. Tool names
#: are a bounded set, so this does not risk cardinality growth.
SAFETY_SCREEN_TOTAL = Counter(
    "orrery_safety_screen_total",
    "Prompt-injection screening engagements by direction and source",
    ["direction", "source"],
)

#: Confirmation lifecycle (AEP-024).
#:
#: The gate already enforced requester-verified approval correctly; what it did
#: not do was leave a record. ``refused`` is the reason these exist: a second
#: person attempting to approve someone else's destructive action is the most
#: interesting event this subsystem can produce, and it left no trace anywhere.
CONFIRMATIONS_RAISED_TOTAL = Counter(
    "orrery_confirmations_raised_total",
    "Guarded tool calls paused for human approval",
    ["tool", "mode"],
)

CONFIRMATIONS_DECIDED_TOTAL = Counter(
    "orrery_confirmations_decided_total",
    "Confirmation decisions accepted by the gate",
    ["tool", "decision"],
)

CONFIRMATIONS_REFUSED_TOTAL = Counter(
    "orrery_confirmations_refused_total",
    "Confirmation decisions rejected by the gate",
    ["tool", "reason"],
)

CONFIRMATIONS_EXPIRED_TOTAL = Counter(
    "orrery_confirmations_expired_total",
    "Pending confirmations that aged out unanswered",
)

#: How long humans actually take to decide. Useful capacity data for tuning the
#: pending TTL: a p95 above the TTL means operators are routinely losing
#: actions to expiry rather than deciding late.
CONFIRMATION_DECISION_SECONDS = Histogram(
    "orrery_confirmation_decision_seconds",
    "Seconds between raising a confirmation and an accepted decision",
    ["tool"],
    buckets=(1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)

# ── Module-level server guard ─────────────────────────────────────────
# Multiple MetricsCollector instances may exist (one per agent module).
# The HTTP server should start exactly once per process.

_server_started = False
_server_lock = threading.Lock()

# ── Internal timing key stored on tool_context state ──────────────────

_TIMING_KEY = "_metrics_invocation_id"

# Bounded status values to prevent cardinality explosion.
_ALLOWED_STATUSES = frozenset({"ok", "success", "error", "confirmation_required"})
_DEFAULT_STATUS = "ok"


def _normalise_status(raw: str) -> str:
    """Map tool response status to a bounded set of label values."""
    return raw if raw in _ALLOWED_STATUSES else _DEFAULT_STATUS


class MetricsCollector:
    """Collects Prometheus metrics via ADK agent callbacks.

    Tracks tool call counts, durations, error rates, and optionally
    circuit breaker state.  Thread-safe.

    Args:
        circuit_breaker: Optional ``CircuitBreaker`` instance whose state
            will be exported as a Prometheus gauge.
    """

    def __init__(self, circuit_breaker=None) -> None:
        self._circuit_breaker = circuit_breaker
        # Keyed by unique invocation ID so concurrent calls to the same
        # tool in the same session don't collide.
        self._timers: dict[str, float] = {}
        self._timers_lock = threading.Lock()

    # ── ADK callbacks ─────────────────────────────────────────────────

    def before_tool_callback(self) -> Callable:
        """Return a before_tool_callback that starts timing."""
        collector = self

        def callback(
            tool: BaseTool,
            args: dict[str, Any],
            tool_context: Context,
        ) -> dict | None:
            invocation_id = uuid.uuid4().hex
            # Store the ID in ADK state so after/error callbacks can find it.
            # ADK State supports __setitem__ but not pop/del, so we overwrite
            # with a sentinel in the after callback rather than deleting.
            tool_context.state[_TIMING_KEY] = invocation_id
            with collector._timers_lock:
                collector._timers[invocation_id] = time.monotonic()

            collector._update_circuit_breaker_gauge(tool.name)

            return None

        return callback

    def after_tool_callback(self) -> Callable:
        """Return an after_tool_callback that records duration and status."""
        collector = self

        def callback(
            *,
            tool: BaseTool,
            args: dict[str, Any],
            tool_context: Context,
            tool_response: dict,
        ) -> dict | None:
            agent_name = (
                tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown"
            )

            # Record duration
            duration = collector._pop_timer(tool_context)
            if duration is not None:
                TOOL_DURATION_SECONDS.labels(agent=agent_name, tool=tool.name).observe(duration)

            # Record call count with bounded status
            raw_status = _DEFAULT_STATUS
            if isinstance(tool_response, dict):
                raw_status = tool_response.get("status", _DEFAULT_STATUS)
            TOOL_CALLS_TOTAL.labels(
                agent=agent_name, tool=tool.name, status=_normalise_status(raw_status)
            ).inc()

            collector._update_circuit_breaker_gauge(tool.name)

            return None

        return callback

    def on_tool_error_callback(self) -> Callable:
        """Return an on_tool_error_callback that records error metrics."""
        collector = self

        def callback(
            tool: BaseTool,
            args: dict[str, Any],
            tool_context: Context,
            error: Exception,
        ) -> None:
            agent_name = (
                tool_context.agent_name if hasattr(tool_context, "agent_name") else "unknown"
            )
            error_type = type(error).__name__

            TOOL_CALLS_TOTAL.labels(agent=agent_name, tool=tool.name, status="error").inc()
            TOOL_ERRORS_TOTAL.labels(agent=agent_name, tool=tool.name, error_type=error_type).inc()

            # Record duration even on error
            duration = collector._pop_timer(tool_context)
            if duration is not None:
                TOOL_DURATION_SECONDS.labels(agent=agent_name, tool=tool.name).observe(duration)

            collector._update_circuit_breaker_gauge(tool.name)

            return None

        return callback

    # ── Internal helpers ──────────────────────────────────────────────

    def _pop_timer(self, tool_context: Context) -> float | None:
        """Remove and return the elapsed duration for this invocation, or None."""
        invocation_id = tool_context.state.get(_TIMING_KEY)
        if invocation_id is None:
            return None
        with self._timers_lock:
            start = self._timers.pop(invocation_id, None)
        if start is None:
            return None
        return time.monotonic() - start

    def _update_circuit_breaker_gauge(self, tool_name: str) -> None:
        """Update the circuit breaker state gauge if a breaker is configured."""
        if self._circuit_breaker is None:
            return
        state = self._circuit_breaker.state(tool_name)
        state_map = {"closed": 0, "open": 1, "half_open": 2}
        CIRCUIT_BREAKER_STATE.labels(tool=tool_name).set(state_map.get(state.value, 0))

    @staticmethod
    def start_server(port: int | None = None) -> None:
        """Start the Prometheus HTTP metrics server.

        Safe to call multiple times — only the first call in the process
        starts the server.

        Args:
            port: TCP port to listen on.  Defaults to the ``METRICS_PORT``
                environment variable, or ``9100`` if not set.
        """
        global _server_started  # noqa: PLW0603
        with _server_lock:
            if _server_started:
                return
            resolved_port = port if port is not None else int(os.getenv("METRICS_PORT", "9100"))
            start_http_server(resolved_port)
            _server_started = True
            logger.info("Prometheus metrics server started on port %d", resolved_port)


def track_llm_tokens(agent_name: str, input_tokens: int, output_tokens: int) -> None:
    """Record LLM token usage.

    Call this from custom LLM wrappers or model callbacks to track
    token consumption per agent.

    Args:
        agent_name: Name of the agent making the LLM call.
        input_tokens: Number of input/prompt tokens.
        output_tokens: Number of output/completion tokens.
    """
    LLM_TOKENS_TOTAL.labels(agent=agent_name, direction="input").inc(input_tokens)
    LLM_TOKENS_TOTAL.labels(agent=agent_name, direction="output").inc(output_tokens)


def track_llm_failover(*, from_model: str, to_model: str) -> None:
    """Record that a model call fell back to the next model in the chain.

    A non-zero rate is a provider incident in progress; a steady one is a
    primary that should be replaced. Labelled by model name — the chain is
    configuration, so the label set is small and fixed.
    """
    LLM_FAILOVER_TOTAL.labels(from_model=from_model, to_model=to_model).inc()


def track_cache_event(*, hit: bool) -> None:
    """Record a context cache hit or miss.

    Call this from model callbacks or middleware that can detect whether
    the LLM response used a cached context prefix.

    Args:
        hit: True for a cache hit, False for a miss.
    """
    CONTEXT_CACHE_EVENTS_TOTAL.labels(event="hit" if hit else "miss").inc()


def track_compaction_event() -> None:
    """Record that conversation history was compacted into a summary.

    Worth watching for two reasons: a session compacting repeatedly is one that
    would otherwise have blown the context window, and each compaction
    invalidates the context-cache prefix (the history it covers changed), so a
    threshold tuned too low quietly erodes the cache-hit rate.

    Unlabelled: compaction is driven by the ADK Runner and the summarizer that
    observes it has no agent identity to attribute the event to.
    """
    CONTEXT_COMPACTION_TOTAL.inc()


def track_safety_screen(*, direction: str, source: str, spans: int = 1) -> None:
    """Record that prompt-injection screening engaged.

    The counter measures *the control working*, not a breach: a direct hit was
    refused before it cost a token, and an indirect one had only the matched
    span replaced. Alert on it anyway — a non-zero rate means instruction-shaped
    text is reaching the agent from somewhere, and the interesting question is
    where.

    Args:
        direction: ``"direct"`` (user message blocked) or ``"indirect"``
            (tool result neutralized).
        source: Tool name for indirect hits, ``"user_message"`` for direct.
        spans: Number of spans neutralized. One for a blocked message, since
            the run stops at the first match.
    """
    SAFETY_SCREEN_TOTAL.labels(direction=direction, source=source).inc(spans)


def track_confirmation_raised(*, tool: str, mode: str) -> None:
    """Record a guarded call paused for approval."""
    CONFIRMATIONS_RAISED_TOTAL.labels(tool=tool, mode=mode).inc()


def track_confirmation_decided(*, tool: str, decision: str, latency_s: float | None) -> None:
    """Record an accepted decision, and how long the human took.

    ``latency_s`` is ``None`` when the pending's creation time is unknown (the
    model-mediated flow has no recorded pending), in which case only the
    counter moves — an invented latency would poison the histogram.
    """
    CONFIRMATIONS_DECIDED_TOTAL.labels(tool=tool, decision=decision).inc()
    if latency_s is not None and latency_s >= 0:
        CONFIRMATION_DECISION_SECONDS.labels(tool=tool).observe(latency_s)


def track_confirmation_refused(*, tool: str, reason: str) -> None:
    """Record a decision the gate rejected.

    ``reason`` is a bounded enum (``not_requester``, ``unknown_requester``,
    ``stale_decision``, ``no_pending``) rather than free text, so it is
    aggregatable and cannot explode label cardinality.
    """
    CONFIRMATIONS_REFUSED_TOTAL.labels(tool=tool, reason=reason).inc()


def track_confirmations_expired(count: int = 1) -> None:
    """Record pending confirmations that aged out unanswered."""
    if count > 0:
        CONFIRMATIONS_EXPIRED_TOTAL.inc(count)
