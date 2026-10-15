# AEP-009: Streaming & Real-Time Agent Responses

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">in-progress</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | High (5-7 days) |
| **Impact** | Medium |
| **Dependencies** | None |

## Status (2026-10)

**Shipped on every transport** (console, Slack, Google Chat). Provider-specific token streaming is still to be checked.

- `POST /chat/stream` (`core/orrery_core/serving/sse.py`) narrates a turn as SSE:
  `session` first (so a client that drops can reload), then `progress`, `delta`
  and `message`, and `done` last. Keepalive comments cover long tool calls.
- `ProgressPlugin` (`core/orrery_core/plugins/progress_plugin.py`) is the
  "Step 2" below, with one correction to the original sketch. Progress comes
  from a plugin armed per request through a ContextVar, not from
  `on_event_callback`. `AgentTool` runs each specialist in its own Runner and
  consumes its events, so the root's event stream shows a whole triage sweep as
  one opaque call per specialist. A plugin is inherited by the sub-Runner and
  sees the specialist's own tool calls. It streams names and outcomes only,
  never arguments or results.
- `AgentGateway.run(..., streaming=True)` runs ADK in `StreamingMode.SSE`. The
  reply is built from non-partial events only, so a streamed turn returns the
  same text as a plain one.
- The turn runs in its own task, so a client that disconnects never cancels an
  agent mid-tool.
- The console renders a live per-specialist trace, then the answer as it is
  written (see [Web console](../integrations/web-console.md)).

- **Slack** (Step 4) edits one in-thread status message while a slow turn
  runs, using the same `ProgressPlugin` feed folded by
  `orrery_core.serving.progress.ProgressTracker`. It leaves a one-line receipt
  when the turn ends (`agents/slack-bot/slack_bot/progress.py`).

- **Google Chat**: the existing "Investigating…" card now lists each specialist and
  its tool calls from the same feed. Flushes are scheduled by the sink, because the
  root emits no events while a specialist works.

**Still open:** a provider-by-provider check of LiteLLM token streaming
(Claude/OpenAI). Every transport already shows tool progress for any provider. The gateway
falls back to whole-answer delivery for any model that does not stream, so
those providers already work; they just don't stream token by token yet.

## Gap Analysis

### Current Implementation
Agent responses are returned as complete messages after all processing is done.
For long-running operations (incident triage with parallel health checks),
the user sees nothing until the entire workflow completes.

The Slack bot receives events via `runner.run_async()` and posts them as complete
messages. The ADK web UI similarly shows final results.

### What ADK Provides
ADK has comprehensive **Streaming** support:
- **Server-Sent Events (SSE)**: `/run_sse` endpoint for real-time event streaming
- **Gemini Live API Toolkit**: Bidirectional streaming for audio/video
- **Partial responses**: LLM tokens streamed as they're generated
- **Event callbacks**: `on_event_callback` in plugins for processing events in flight
- **Streaming tools**: Tools can yield intermediate results

### Gap
DevOps operations are often long-running:
- Incident triage may take 30-60 seconds across all health checks
- Kafka topic creation involves broker coordination
- Pod log retrieval may take time for large logs

Users see a blank screen during this time, which creates uncertainty about whether
the agent is working or stuck.

## Proposed Solution

### Step 1: Enable SSE for the Web Runner

Use ADK's `/run_sse` endpoint instead of the batch `/run` endpoint for the web UI:

```python
# The ADK API server already supports this
# adk api_server agents/orrery-assistant --port 8000
# Client connects to /run_sse for streaming
```

### Step 2: Add Progress Events in Plugins

Use `on_event_callback` in a plugin to emit progress updates:

```python
class ProgressPlugin(BasePlugin):
    """Emits progress events for long-running operations."""

    def __init__(self):
        super().__init__(name="progress")

    async def before_tool_callback(self, *, tool, args, tool_context):
        # Emit a "working on..." event before each tool call
        logger.info(f"Starting: {tool.name}")
        return None  # Don't block execution

    async def on_event_callback(self, *, invocation_context, event):
        # Add timing metadata to events
        event.custom_metadata = event.custom_metadata or {}
        event.custom_metadata["timestamp"] = datetime.now().isoformat()
        return event
```

### Step 3: Stream Parallel Health Check Progress

During incident triage, stream results as each health check completes:

```python
# Each parallel sub-agent's output_key update becomes a streamed event
# The user sees:
# "Checking Kafka health... done (3 brokers online)"
# "Checking K8s health... done (12 pods running)"
# "Checking Docker... done (5 containers healthy)"
# "Checking Observability... done (2 alerts firing)"
# "Generating triage summary..."
```

### Step 4: Update Slack Bot for Streaming

Post incremental updates to Slack threads:

```python
async def handle_streaming_events(self, runner, session, message):
    async for event in runner.run_async(...):
        if event.content and event.content.parts:
            # Update the Slack message in-place or add thread replies
            await self.update_message(event)
```

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/plugins.py` | Add `ProgressPlugin` |
| `agents/orrery-assistant/orrery_assistant/agent.py` | Configure SSE support |
| `agents/slack-bot/slack_bot/handler.py` | Stream events as Slack thread replies |
| `Makefile` | Update `run-assistant` to use `adk api_server` with SSE |

## Acceptance Criteria

- [x] Agent responses stream in real-time via SSE (`POST /chat/stream`)
- [x] Parallel health check progress visible as each check completes (including calls inside specialists)
- [x] Slack bot posts incremental updates during long operations
- [x] `ProgressPlugin` reports timing for each tool call
- [x] No regression in final response quality (identical reply text, pinned by tests)
- [ ] Streaming works with all LLM providers (Gemini, Claude, OpenAI)

## Notes

- Streaming with LiteLLM (Claude/OpenAI) may require additional configuration. Test thoroughly with each provider.
- The Gemini Live API Toolkit for audio/video streaming is likely not relevant for a DevOps CLI/Slack interface, but could be useful for a future voice-based ops assistant.
- SSE connections can be dropped by proxies/load balancers. Document timeout settings for production deployments.
