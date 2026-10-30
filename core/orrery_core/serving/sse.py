"""Server-Sent Events for one agent turn (AEP-009).

``POST /chat`` answers once the whole turn is over, which for a triage sweep
across five specialists is a minute of nothing. :func:`stream_turn` runs the
same turn and narrates it as it happens:

========== ==========================================================
event      data
========== ==========================================================
session    ``{"session_id"}`` — first, so a client that drops can reload
progress   a :class:`~orrery_core.plugins.ProgressStep` — tool start/end,
           including calls made inside ``AgentTool`` specialists
delta      ``{"text", "author"}`` — answer text as the model writes it
message    ``{"session_id", "text"}`` — the complete reply; authoritative
           (a client should replace its accumulated deltas with it)
error      ``{"message", "retryable"}`` — the turn failed
done       ``{}`` — always last
========== ==========================================================

A ``: keepalive`` comment goes out when nothing else has for a while, so idle
proxies and load balancers do not cut a long tool call off mid-turn.

**The turn outlives the connection.** It runs in its own task, not in the
response generator: if the client goes away, Starlette cancels the generator,
and cancelling an agent mid-tool would leave a mutation's outcome unknown and
a pending confirmation orphaned. The console's *Stop* button has always meant
"stop waiting", and the server finishing the turn is what makes reloading the
conversation afterwards show its answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any, Protocol

from google.adk.events import Event

from .. import learning_gate
from ..plugins.progress_plugin import progress_scope
from .events import extract_reply_text
from .gateway import (
    TURN_TIMED_OUT_MESSAGE,
    EventHook,
    InboundMessage,
    OutboundReply,
    TurnTimedOut,
)

logger = logging.getLogger("orrery.serving.sse")

#: Seconds of silence before a keepalive comment is sent.
DEFAULT_KEEPALIVE_SECONDS = 15.0

KEEPALIVE = b": keepalive\n\n"

#: Shown to the user when a turn fails. Deliberately generic: the exception can
#: carry provider error bodies, hostnames or tool output, none of which belong
#: in a browser. The traceback goes to the server log.
TURN_FAILED_MESSAGE = "The agent hit an error while handling this message. Please try again."


# Strong references to running turns. asyncio keeps only a weak reference to a
# task, so a turn whose client disconnected — nobody awaiting it any more —
# could otherwise be garbage-collected mid-run.
_running_turns: set[asyncio.Task[None]] = set()


async def drain_turns(timeout: float) -> int:
    """Wait up to *timeout* seconds for running turns; return how many are left.

    Called at shutdown. A turn outlives its client by design (a closed tab must
    not cut a tool call in half), and the server stops waiting for a request
    once its client is gone, so without this a rolling deploy's SIGTERM ended
    those turns mid-tool: exactly the outcome they were kept running to avoid.
    """
    pending = set(_running_turns)
    if not pending:
        return 0
    logger.info("Waiting up to %.0fs for %d running turn(s)", timeout, len(pending))
    _, still = await asyncio.wait(pending, timeout=timeout)
    if still:
        logger.warning("Shutting down with %d turn(s) still running", len(still))
    return len(still)


class TurnRunner(Protocol):
    """What :func:`stream_turn` needs from a gateway — ``AgentGateway`` is one."""

    async def run(
        self,
        msg: InboundMessage,
        *,
        on_event: EventHook | None = None,
        session_id: str | None = None,
        streaming: bool = False,
    ) -> OutboundReply: ...


def encode_event(event: str, data: dict[str, Any]) -> bytes:
    """One SSE frame. ``data`` is compact JSON on a single line."""
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


async def stream_turn(
    gateway: TurnRunner,
    msg: InboundMessage,
    *,
    session_id: str,
    keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
) -> AsyncGenerator[bytes]:
    """Run *msg* as one turn in *session_id* and yield its SSE frames.

    *session_id* must already be resolved (``gateway.resolve_session``), so a
    failure to resolve it surfaces as an ordinary HTTP error rather than an
    ``error`` event on a response that has already said 200.
    """
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    # Streamed text cannot be retracted, and the answer guard checks only a
    # final response, so the learning sandbox sends the checked ``message``
    # alone (AEP-032 §4). Read once: activation happens before serving.
    hold_deltas = learning_gate.is_active()

    def _step(step: Any) -> dict[str, Any]:
        frame = step.as_dict()
        if hold_deltas:
            # A tool's own status string could be a string target (OOMKilled).
            frame["status"] = ""
        return frame

    async def on_event(event: Event) -> None:
        if hold_deltas:
            return
        if getattr(event, "partial", None) is True and (text := extract_reply_text(event)):
            queue.put_nowait(("delta", {"text": text, "author": event.author or ""}))

    async def run() -> None:
        try:
            # Entered inside the task that runs the agent, so the sink reaches
            # every specialist's sub-Runner (ContextVars copy into child tasks).
            with progress_scope(lambda step: queue.put_nowait(("progress", _step(step)))):
                reply = await gateway.run(
                    msg, session_id=session_id, on_event=on_event, streaming=True
                )
            queue.put_nowait(("message", {"session_id": session_id, "text": reply.text}))
        except TurnTimedOut:
            queue.put_nowait(("error", {"message": TURN_TIMED_OUT_MESSAGE, "retryable": False}))
        except Exception:
            logger.exception("Streaming turn failed (session %s)", session_id)
            queue.put_nowait(("error", {"message": TURN_FAILED_MESSAGE, "retryable": True}))
        finally:
            queue.put_nowait(None)

    yield encode_event("session", {"session_id": session_id})

    task = asyncio.create_task(run(), name=f"orrery-turn-{session_id}")
    _running_turns.add(task)
    task.add_done_callback(_running_turns.discard)

    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=keepalive_seconds)
        except TimeoutError:
            yield KEEPALIVE
            continue
        if item is None:
            yield encode_event("done", {})
            return
        yield encode_event(*item)
