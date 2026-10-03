"""CallBudgetPlugin — bound how many tool calls one run may make.

Nothing counted a run's tool calls. The circuit breaker counts a tool's
*failures*, the output cap the *bytes* that come back, the repeat guard the same
call failing the same way — and a search that keeps rephrasing itself trips
none of them, because every call is new and most of them succeed.

That shape is the expensive one. Each model call in a run re-sends the whole run
so far, so cost grows with the square of the run's length, while the answer at
call 90 is rarely better than the one the evidence supported at call 30. In
practice a small share of runs — almost all of them searches going round —
accounts for a large share of the tokens.

**Charged per run** (see :mod:`orrery_core.plugins.run_scope`): an ``AgentTool``
specialist gets its own budget, and so does the root's turn. **A call a gate
refused does not count** — it never ran — so the budget is charged in
``after_tool_callback``, once the outcome is known.

**Two steps, so a run ends with an answer rather than mid-search.**

* From :data:`WARN_FRACTION` of the budget, every model request carries a short
  note asking the model to converge. It is appended as the *last* content of
  the request, never written into a tool result: ADK's after-tool chain stops at
  the first callback that returns a value, so rewriting results here could skip
  the plugins after this one, and appending at the end leaves the cached
  context prefix untouched.
* At the budget, ``before_tool_callback`` answers the call itself with a
  structured result telling the model to answer with what it has. Every
  further call in the run is answered the same way. Audit, registered first,
  still records each attempt.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from ..reliability.resilience import ToolOutcome, classify_tool_outcome
from .run_scope import RunLedger, run_key

logger = logging.getLogger("orrery.plugins.call_budget")

#: The ``status`` the model sees in place of a call over the budget. Neutral for
#: the circuit breaker (the tool never ran): see ``_NEUTRAL_STATUSES``.
CALL_BUDGET_STATUS = "CALL_BUDGET_EXHAUSTED"

#: Default tool calls one run may make. Generous for a targeted question or a
#: full triage sweep; a run past it is almost always a search going round.
DEFAULT_MAX_TOOL_CALLS_PER_RUN = 50

#: Share of the budget after which every model request carries the note.
WARN_FRACTION = 0.7


class CallBudgetPlugin(BasePlugin):
    """Refuse tool calls past ``max_calls`` in one run; warn the model before.

    Args:
        max_calls: Tool calls one run may make. ``0`` disables the plugin.
        warn_fraction: Share of the budget after which the model is warned.
    """

    def __init__(
        self,
        *,
        max_calls: int = DEFAULT_MAX_TOOL_CALLS_PER_RUN,
        warn_fraction: float = WARN_FRACTION,
    ) -> None:
        super().__init__(name="call_budget")
        self._max = max_calls
        self._warn_at = max(1, int(max_calls * warn_fraction))
        self._spent: RunLedger[list[int]] = RunLedger(lambda: [0])

    def spent(self, run: str) -> int:
        """Calls charged to *run* so far (for tests and diagnostics)."""
        entry = self._spent.peek(run)
        return entry[0] if entry else 0

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        if self._max <= 0:
            return None
        run = run_key(callback_context)
        spent = self.spent(run) if run else 0
        if spent < self._warn_at:
            return None
        left = max(0, self._max - spent)
        note = (
            f"[Tool-call budget: {spent} of {self._max} calls used in this run, "
            f"{left} left. Stop exploring: answer with the evidence you already "
            "have, say plainly what you could not check, and suggest how the "
            "user could narrow the question.]"
        )
        llm_request.contents.append(types.Content(role="user", parts=[types.Part(text=note)]))
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        if self._max <= 0:
            return None
        run = run_key(tool_context)
        if not run or self.spent(run) < self._max:
            return None
        logger.warning(
            "Call budget exhausted: refused '%s' (run=%s, budget=%d)", tool.name, run, self._max
        )
        return {
            "status": CALL_BUDGET_STATUS,
            "error": (
                f"This run has made {self._max} tool calls, its limit, so "
                f"'{tool.name}' was not called and no further tool will be. "
                "Answer now with the evidence you already have. Say what you "
                "could not check, and suggest a narrower question (one "
                "namespace, one service, one time window) rather than retrying."
            ),
        }

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> None:
        if self._max <= 0:
            return None
        run = run_key(tool_context)
        if run and classify_tool_outcome(result) is not ToolOutcome.IGNORE:
            self._spent.get(run)[0] += 1
        return None
