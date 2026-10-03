"""DelegationGuardPlugin — stop a coordinator re-asking a specialist the same thing.

Delegation is a tool call like any other, and it is the most expensive one an
agent makes: an ``AgentTool`` invocation builds its own Runner and session,
re-sends the specialist's whole instruction and tool declarations, and runs an
agentic loop of its own. One of them can cost more than the rest of the turn.

**The loop it catches is made of successful calls.** A specialist answers
"blocked: awaiting approval" or "that namespace does not exist", the
coordinator does not believe it, and re-delegates with escalating wording —
"you are wrong, the user already approved, execute it now". Every one of those
calls *succeeds* (the specialist answered), and every one carries different
prose, so neither the circuit breaker (which counts failures) nor the repeat
guard (which matches exact arguments) sees anything. The coordinator can spend
the whole turn, and the user's patience, arguing with its own specialist.

So this guard counts the one thing neither of them does: **how many times one
run has called one specialist, at all.** Not a failure rate, not a fingerprint —
a budget. It is deliberately blunt, because the failure it catches is not
subtle.

**Per run** (one agent's work in one invocation, see
:mod:`orrery_core.plugins.run_scope`): a conversation that asks the k8s
specialist five different things across five messages is untouched. A healthy
turn can need the same specialist more than once (look, act, verify), which is
why the default leaves room for that.

**Refusing well.** The refusal is addressed to the coordinator: the specialist
already answered, re-asking will not change the answer, and the way forward is
to tell the user what is blocking — because in the case this exists for, the
specialist was usually right.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from .run_scope import RunLedger, run_key

logger = logging.getLogger("orrery.plugins.delegation_guard")

#: The ``status`` the coordinator sees in place of a refused delegation. Neutral
#: for the circuit breaker (the specialist never ran): see ``_NEUTRAL_STATUSES``.
DELEGATION_REFUSED_STATUS = "DELEGATION_BUDGET_EXHAUSTED"

#: Delegations to **one** specialist that a single run may make: room for
#: look → act → verify, and well short of an argument with the specialist.
DEFAULT_MAX_DELEGATIONS_PER_RUN = 4


class DelegationGuardPlugin(BasePlugin):
    """Refuse an ``AgentTool`` call past ``max_per_agent`` in one run.

    Args:
        max_per_agent: Calls to one specialist a run may make. ``0`` disables.
    """

    def __init__(self, *, max_per_agent: int = DEFAULT_MAX_DELEGATIONS_PER_RUN) -> None:
        super().__init__(name="delegation_guard")
        self._max = max_per_agent
        self._counts: RunLedger[dict[str, int]] = RunLedger(dict)

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        if self._max <= 0 or not isinstance(tool, AgentTool):
            return None
        run = run_key(tool_context)
        if not run:
            return None
        counts = self._counts.get(run)
        made = counts.get(tool.name, 0)
        if made < self._max:
            counts[tool.name] = made + 1
            return None
        logger.warning(
            "Refused delegation to '%s': already called %d times this run", tool.name, made
        )
        return {
            "status": DELEGATION_REFUSED_STATUS,
            "error": (
                f"'{tool.name}' has already been asked {made} times in this turn and "
                "has answered each time. Asking again will not change its answer. "
                "Do not re-delegate: tell the user what it reported, including "
                "anything it said is blocking (an approval waiting, a missing "
                "permission, something that does not exist), and what they can do "
                "about it."
            ),
        }
