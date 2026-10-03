"""ToolLedgerPlugin — what one turn actually did, including inside sub-agents.

A transport sometimes has to answer questions about a turn that its own event
stream cannot: *did anything change before this run was cut short?* and *was
this run refused something it needed?* Reading ``function_response`` parts off
the events the Runner yields covers the root agent and nothing else, because
``AgentTool`` builds its **own** Runner and consumes the child's events
internally, forwarding only the final text. A guarded call made by the k8s
specialist on the chat root's behalf is invisible from outside it.

A plugin crosses that boundary where an event reader cannot: ``AgentTool``
hands the parent's plugin list to the sub-Runner (``include_plugins=True`` is
ADK's default), so these callbacks run inside the specialist's invocation too.

**Armed by the caller, inert otherwise.** The ledger lives in a ``ContextVar``
the caller sets before the run (:func:`tool_ledger_scope`); the plugin only
ever *appends* to the object already there, which is what makes the record
visible to the caller — rebinding a ContextVar inside a child task does not
propagate out, mutating the list it holds does. Un-armed, every callback
returns immediately, so a process that never asks pays nothing for it.

**What counts as a mutation.** A tool marked ``@confirm`` or ``@destructive``
— the same metadata RBAC and the confirmation gate read — that got past every
gate. Two cases are recorded as *executed*:

* the call **completed** with any outcome other than a gate's refusal
  (``classify_tool_outcome`` → SUCCESS or FAILURE). A failed call usually
  changed nothing, but "usually" is not a property a caller deciding whether
  to retry can lean on;
* the call **started and never finished** — the run was cancelled while the
  tool was running. Its effect is unknown, which for the same reason has to
  be reported as "may have happened", not dropped.

A call a gate refused (``BLOCKED``, ``AWAITING_CONFIRMATION``,
``access_denied``, ``confirmation_required``) never ran; it is recorded as a
*refusal* instead, which is the other half of what a caller usually needs.

Registered right after ``AuditPlugin``, ahead of the gates: the before-tool
chain early-exits on the first plugin that answers, so anything registered
after a gate would never see the call it refused start.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..reliability.resilience import ToolOutcome, classify_tool_outcome
from ..security.guardrails import is_guarded

logger = logging.getLogger("orrery.plugins.tool_ledger")


@dataclass
class LedgerEntry:
    """One tool call as the ledger saw it.

    Attributes:
        tool: The tool's name.
        status: The result's ``status`` field, ``""`` when it had none, or
            ``"in_flight"`` for a call that started and never finished.
        guarded: Whether the tool is ``@confirm``/``@destructive``.
        refused: Whether a gate answered instead of the tool.
    """

    tool: str
    status: str
    guarded: bool
    refused: bool


@dataclass
class ToolLedger:
    """The calls one run made. Created by the caller, appended to by the plugin."""

    entries: list[LedgerEntry] = field(default_factory=list)
    #: Guarded calls that started but have not completed, keyed by call id.
    in_flight: dict[str, str] = field(default_factory=dict)

    def mutations(self) -> list[str]:
        """Guarded tools that ran or may have run, in call order.

        Includes calls still in flight: a run cancelled mid-tool leaves the
        effect of that tool unknown, and a caller deciding whether a retry is
        safe has to assume it landed.
        """
        ran = [e.tool for e in self.entries if e.guarded and not e.refused]
        return ran + list(self.in_flight.values())

    def refusals(self) -> list[tuple[str, str]]:
        """``(tool, status)`` for every call a gate answered instead of the tool."""
        return [(e.tool, e.status) for e in self.entries if e.refused]

    def calls(self) -> list[str]:
        """Every tool that completed or was refused, in order."""
        return [e.tool for e in self.entries]


_LEDGER: contextvars.ContextVar[ToolLedger | None] = contextvars.ContextVar(
    "orrery_tool_ledger", default=None
)


@contextmanager
def tool_ledger_scope(ledger: ToolLedger | None = None) -> Iterator[ToolLedger]:
    """Arm a ledger for the code inside the block and yield it.

    Enter it in the task that runs the agent (``asyncio.create_task`` and
    ``run_coroutine_threadsafe`` copy the *current* context, so arming it in
    another thread or a parent that spawns the task later does not reach it).
    """
    ledger = ledger if ledger is not None else ToolLedger()
    token = _LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _LEDGER.reset(token)


def current_tool_ledger() -> ToolLedger | None:
    """The ledger armed for the current context, or ``None``."""
    return _LEDGER.get()


def _status_of(result: Any) -> str:
    value = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
    return value if isinstance(value, str) else ""


def _call_key(tool: BaseTool, tool_context: ToolContext) -> str:
    """Identify one call: ADK's function-call id, else the context object."""
    call_id = getattr(tool_context, "function_call_id", None)
    return str(call_id) if call_id else f"{tool.name}:{id(tool_context)}"


class ToolLedgerPlugin(BasePlugin):
    """Record executed mutations and refusals into the caller's armed ledger."""

    def __init__(self) -> None:
        super().__init__(name="tool_ledger")

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> None:
        ledger = _LEDGER.get()
        if ledger is not None and is_guarded(tool):
            ledger.in_flight[_call_key(tool, tool_context)] = tool.name
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> None:
        ledger = _LEDGER.get()
        if ledger is None:
            return None
        ledger.in_flight.pop(_call_key(tool, tool_context), None)
        refused = classify_tool_outcome(result) is ToolOutcome.IGNORE
        ledger.entries.append(
            LedgerEntry(
                tool=tool.name,
                status=_status_of(result),
                guarded=is_guarded(tool),
                refused=refused,
            )
        )
        return None
