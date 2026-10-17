"""ProgressPlugin — live tool-call progress for a streaming transport.

A streaming client wants to show *what the agent is doing* while a turn runs:
"asking the Kafka specialist… it is reading consumer lag… done". The Runner's
own event stream cannot supply that past the first level. ``AgentTool`` runs
each specialist in its **own** Runner and consumes the child's events, so from
outside, a triage sweep is one opaque call per specialist that ends a minute
later. A plugin crosses that boundary — ``AgentTool`` hands the parent's plugin
list to the sub-Runner — which is the same reason ``ToolLedgerPlugin`` exists.

**Armed by the caller, inert otherwise.** A transport arms a sink for the code
inside :func:`progress_scope`; un-armed, every callback returns at once. The
sink lives in a ``ContextVar``, so it reaches every task the turn spawns as long
as the scope is entered in the task that runs the agent.

**Names and outcomes, never arguments.** A step carries the agent, the tool, a
coarse status and a duration. Tool arguments and results stay out: they are
attacker-reachable text (pod annotations, log lines) and can hold credentials
that ``PIIRedactionPlugin`` only scrubs from *results*, so streaming them to a
browser would be a second, unscreened output channel.

Registered next to ``ToolLedgerPlugin``, ahead of the gates, for the same
reason: the before-tool chain stops at the first plugin that answers, so one
registered after a gate never sees the call that gate refused start.
"""

from __future__ import annotations

import contextvars
import logging
import re
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..reliability.resilience import ToolOutcome, classify_tool_outcome
from ..security.guardrails import is_guarded

logger = logging.getLogger("orrery.plugins.progress")

#: What a step ended as, from the transport's point of view.
StepOutcome = Literal["ok", "error", "refused"]


@dataclass(frozen=True)
class ProgressStep:
    """One edge of one tool call: its start, or its end.

    Attributes:
        call_id: Stable id pairing a ``start`` with its ``end``.
        phase: ``"start"`` or ``"end"``.
        agent: The agent that made the call (a specialist's name inside an
            ``AgentTool``, the root's name otherwise).
        tool: The tool's name. For a delegation this is the specialist.
        delegation: The call hands work to another agent (an ``AgentTool``);
            later steps whose ``agent`` equals ``tool`` happened inside it.
        guarded: The tool is ``@confirm``/``@destructive``.
        outcome: On ``end`` only — ``ok``, ``error`` (the tool reported
            failure) or ``refused`` (a gate answered instead of the tool:
            access denied, awaiting confirmation, circuit open, budget spent).
        status: On ``end`` only — the result's ``status`` field, if it had one.
        elapsed_ms: On ``end`` only — wall time since ``start``.
    """

    call_id: str
    phase: Literal["start", "end"]
    agent: str
    tool: str
    delegation: bool
    guarded: bool
    outcome: StepOutcome | None = None
    status: str = ""
    elapsed_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


ProgressSink = Callable[[ProgressStep], None]

#: Concurrent calls whose start time is remembered (see ProgressPlugin).
_MAX_TRACKED_CALLS = 1024

_SINK: contextvars.ContextVar[ProgressSink | None] = contextvars.ContextVar(
    "orrery_progress_sink", default=None
)


@contextmanager
def progress_scope(sink: ProgressSink) -> Generator[None]:
    """Deliver every tool call's progress inside the block to *sink*.

    *sink* is called synchronously on the event loop, so it must not block — an
    ``asyncio.Queue.put_nowait`` is the intended shape. A sink that raises is
    logged and ignored: progress is a rendering aid and must never fail a turn.
    """
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def _call_id(tool: BaseTool, tool_context: ToolContext) -> str:
    call_id = getattr(tool_context, "function_call_id", None)
    return str(call_id) if call_id else f"{tool.name}:{id(tool_context)}"


#: What a result's ``status`` may look like to be forwarded. The field is
#: shown in the browser and in chat, and an MCP server controls it, so only a
#: short machine word gets through (``success``, ``confirmation_required``).
_STATUS = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,39}")


def _status_of(result: Any) -> str:
    value = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
    return value if isinstance(value, str) and _STATUS.fullmatch(value) else ""


def _outcome(result: Any) -> StepOutcome:
    match classify_tool_outcome(result):
        case ToolOutcome.IGNORE:
            return "refused"
        case ToolOutcome.FAILURE:
            return "error"
        case _:
            return "ok"


class ProgressPlugin(BasePlugin):
    """Report tool-call start/end to the sink armed by :func:`progress_scope`."""

    def __init__(self) -> None:
        super().__init__(name="progress")
        # Start times by call id, dropped on `end`. A turn cancelled mid-call
        # never reaches `end`, so the dict is capped rather than trusted to
        # drain: past the cap the oldest entries go, and their `end` (if it
        # ever comes) just reports no duration.
        self._started: dict[str, float] = {}

    @staticmethod
    def _emit(sink: ProgressSink, step: ProgressStep) -> None:
        try:
            sink(step)
        except Exception:  # noqa: BLE001 — progress must never fail a turn
            logger.warning("Progress sink raised; dropping step", exc_info=True)

    def _step(
        self, tool: BaseTool, tool_context: ToolContext, phase: Literal["start", "end"]
    ) -> ProgressStep:
        return ProgressStep(
            call_id=_call_id(tool, tool_context),
            phase=phase,
            agent=str(getattr(tool_context, "agent_name", "") or ""),
            tool=tool.name,
            delegation=isinstance(tool, AgentTool),
            guarded=is_guarded(tool),
        )

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> None:
        sink = _SINK.get()
        if sink is None:
            return None
        step = self._step(tool, tool_context, "start")
        if len(self._started) >= _MAX_TRACKED_CALLS:
            # Insertion-ordered: the first key is the oldest start.
            del self._started[next(iter(self._started))]
        self._started[step.call_id] = time.monotonic()
        self._emit(sink, step)
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> None:
        sink = _SINK.get()
        if sink is None:
            return None
        base = self._step(tool, tool_context, "end")
        started = self._started.pop(base.call_id, None)
        elapsed = int((time.monotonic() - started) * 1000) if started is not None else None
        self._emit(
            sink,
            replace(
                base,
                outcome=_outcome(result),
                status=_status_of(result),
                elapsed_ms=elapsed,
            ),
        )
        return None
