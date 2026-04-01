# AEP-010: Distributed Tracing & Observability

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P2 |
| **Effort** | Medium (3-4 days) |
| **Impact** | High |
| **Dependencies** | None |

## Gap Analysis

### Current Implementation
The project has solid observability foundations:
- **Structured JSON logging** via `setup_logging()` with `JSONFormatter`
- **Prometheus metrics** via `MetricsPlugin` (counters, histograms, gauges)
- **Audit logging** via `AuditPlugin` with secret redaction
- **Activity tracking** via `ActivityPlugin` for cross-agent visibility

However, there is **no distributed tracing**:
- No trace IDs linking a user request through the agent hierarchy
- No spans for individual tool calls or LLM invocations
- No correlation between logs, metrics, and traces
- No visualization of agent execution flow

### What ADK Provides
ADK supports multiple **observability integrations**:
- **Google Cloud Trace**: Native integration for span-based tracing
- **OpenTelemetry**: Via third-party integrations (Phoenix, AgentOps, LangWatch, etc.)
- **ADK Web UI Trace Tab**: Built-in execution flow visualization
- **Event-based tracing**: Each `Event` in ADK has metadata for trace reconstruction

The ADK web UI already provides:
- Trace grouping by user message
- Clickable trace rows showing Event, Request, Response, and Graph tabs
- Visual representation of tool calls and agent logic flow

### Gap
For enterprise DevOps:
1. **No request correlation**: Can't follow a user query through Kafka check -> K8s check -> summary
2. **No latency attribution**: Can't determine if slowness is in the LLM call, tool execution, or Kafka client
3. **No cross-agent traces**: When incident triage runs parallel health checks, there's no unified trace
4. **No external trace export**: Traces stay in the ADK web UI, not in Grafana/Jaeger/Tempo

## Proposed Solution

### Step 1: Add OpenTelemetry Instrumentation

Create a `TracingPlugin` that emits OpenTelemetry spans:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

class TracingPlugin(BasePlugin):
    """Emits OpenTelemetry spans for agent lifecycle events."""

    def __init__(self):
        super().__init__(name="tracing")
        self.tracer = trace.get_tracer("ai-agents")

    async def before_agent_callback(self, *, agent, callback_context):
        span = self.tracer.start_span(f"agent.{agent.name}")
        callback_context.state["_trace_span"] = span
        return None

    async def after_agent_callback(self, *, agent, callback_context):
        span = callback_context.state.get("_trace_span")
        if span:
            span.end()
        return None

    async def before_tool_callback(self, *, tool, args, tool_context):
        parent_span = tool_context.state.get("_trace_span")
        ctx = trace.set_span_in_context(parent_span) if parent_span else None
        span = self.tracer.start_span(f"tool.{tool.name}", context=ctx)
        span.set_attribute("tool.args", str(args)[:500])
        tool_context.state["_tool_span"] = span
        return None

    async def after_tool_callback(self, *, tool, args, tool_context, result):
        span = tool_context.state.get("_tool_span")
        if span:
            span.set_attribute("tool.result_size", len(str(result)))
            span.end()
        return None

    async def before_model_callback(self, *, callback_context, llm_request):
        span = self.tracer.start_span("llm.call")
        span.set_attribute("llm.model", str(llm_request.model))
        callback_context.state["_llm_span"] = span
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        span = callback_context.state.get("_llm_span")
        if span:
            if llm_response and llm_response.usage:
                span.set_attribute("llm.input_tokens", llm_response.usage.input_tokens)
                span.set_attribute("llm.output_tokens", llm_response.usage.output_tokens)
            span.end()
        return None
```

### Step 2: Add Request Correlation IDs

Generate a trace ID at the entry point and propagate it:

```python
import uuid

class TracingPlugin(BasePlugin):
    async def on_user_message_callback(self, *, invocation_context, user_message):
        request_id = str(uuid.uuid4())
        invocation_context.session.state["request_id"] = request_id
        # Add to structured logs
        logger.info("request_started", extra={"request_id": request_id})
        return None
```

### Step 3: Add Tracing Infrastructure

```yaml
# docker-compose.yml (additions)
services:
  tempo:
    image: grafana/tempo:latest
    ports:
      - "4317:4317"   # OTLP gRPC
      - "3200:3200"   # Tempo API

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3001:3000"
    environment:
      - GF_FEATURE_TOGGLES_ENABLE=traceqlEditor
    volumes:
      - ./infra/grafana-datasources.yml:/etc/grafana/provisioning/datasources/ds.yml
```

### Step 4: Integrate with Existing Metrics

Correlate trace IDs with Prometheus metrics using exemplars:

```python
# In MetricsPlugin
tool_duration.observe(
    duration,
    exemplar={"trace_id": tool_context.state.get("request_id", "")},
)
```

## Affected Files

| File | Change |
|------|--------|
| `core/ai_agents_core/tracing.py` | New: `TracingPlugin` with OpenTelemetry |
| `core/ai_agents_core/plugins.py` | Add `TracingPlugin` to `default_plugins()` |
| `core/ai_agents_core/log.py` | Add `request_id` to JSON log format |
| `core/pyproject.toml` | Add `opentelemetry-*` dependencies |
| `infra/docker-compose.yml` | Add Tempo + Grafana services |
| `infra/grafana-datasources.yml` | New: Grafana datasource config |
| `docs/metrics.md` | Update with tracing documentation |

## Acceptance Criteria

- [ ] Every user request gets a unique trace ID
- [ ] Agent -> tool -> LLM calls create nested spans
- [ ] Traces exported to Tempo (or configurable OTLP endpoint)
- [ ] Grafana dashboard shows agent execution traces
- [ ] Trace IDs appear in structured JSON logs
- [ ] Prometheus metrics include trace ID exemplars
- [ ] Latency attribution visible (LLM vs tool vs network)
- [ ] TracingPlugin can be disabled via environment variable

## Notes

- OpenTelemetry adds ~5-10ms overhead per span. For DevOps agents where tool calls take seconds, this is negligible.
- Consider using the ADK web UI's built-in Trace tab for development and Grafana/Tempo for production.
- The `TracingPlugin` should be ordered early in `default_plugins()` to capture the full execution lifecycle.
