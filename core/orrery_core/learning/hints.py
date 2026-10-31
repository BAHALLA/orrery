"""Disclosure levels and hints (AEP-032 §3).

Each learner has a level from 0 to 3 per scenario, starting at the
manifest's ``default_disclosure``. Only a person raises it, one step at a
time, through ``POST /learning/hint``, never through a model-callable tool:
a learner could talk the model into calling one.

- **Per process.** :class:`HintStore` is in memory. A restart puts every
  learner back at ``default_disclosure``, which withholds more, never less.
  The durable record of help is the ``learning_hint`` audit stream.
- **Out of band.** The endpoint writes this store only, never a session (an
  append while a turn runs would fail that turn's next event). The server
  copies the level into each turn's ``state_delta`` under a server-set marker
  (``learning_gate.LEARNING_LEVEL_*``); :func:`level_from_state` reads it, and
  ``IdentityStateGuardPlugin`` reverts any tool that writes either key.
- **The model sees the level, never the values.**
  :class:`DisclosureInstructionPlugin` appends one line to the root's system
  instruction.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

from .. import learning_gate
from ..observability.audit import audit_event
from .guard import REVEAL_LEVEL

#: Learners remembered per process; the least recently active one is dropped
#: first and reads as ``default_disclosure`` again (withholds more, not less).
MAX_LEARNERS = 10_000

#: What each level lets the coordinator say: the hint ladder.
LEVEL_RULES = {
    0: (
        "orient: ask questions that lead the learner to the signal worth looking at "
        "(an alert, a metric, logs); name no topic, group, tool or value"
    ),
    1: (
        "narrow: say where to look (which topic, consumer group, alert or service), "
        "but not which tool to run or what it returns"
    ),
    2: (
        "point at the tool: name the tool to run and what to compare in its output, "
        "but leave the conclusion and the value to the learner"
    ),
    3: "reveal: give the answer with its evidence; the exercise values are no longer withheld",
}


class HintStore:
    """``subject -> (scenario_id, level)``, bounded, thread-safe."""

    def __init__(self, *, max_learners: int = MAX_LEARNERS) -> None:
        self._levels: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._max = max_learners
        self._lock = threading.Lock()

    def level(self, subject: str, scenario_id: str, default: int) -> int:
        """The learner's level; one recorded for another scenario reads as *default*."""
        with self._lock:
            entry = self._levels.get(subject)
            if entry is None or entry[0] != scenario_id:
                return default
            self._levels.move_to_end(subject)
            return max(entry[1], default)

    def raise_level(self, subject: str, scenario_id: str, default: int) -> tuple[int, int]:
        """One step up, never past :data:`REVEAL_LEVEL`. Returns ``(old, new)``."""
        with self._lock:
            entry = self._levels.get(subject)
            old = entry[1] if entry is not None and entry[0] == scenario_id else default
            old = max(old, default)
            new = min(old + 1, REVEAL_LEVEL)
            self._levels[subject] = (scenario_id, new)
            self._levels.move_to_end(subject)
            while len(self._levels) > self._max:
                self._levels.popitem(last=False)
            return old, new


def state_for(scenario_id: str, level: int) -> dict[str, Any]:
    """The two keys the server stamps into a turn's ``state_delta``."""
    return {
        learning_gate.LEARNING_LEVEL_STATE_KEY: {"scenario_id": scenario_id, "level": level},
        learning_gate.LEARNING_LEVEL_LOCKED_STATE_KEY: True,
    }


def level_from_state(state: Any, scenario_id: str, default: int) -> int:
    """The turn's level, or *default* for a bare, foreign or malformed value.

    *state* is anything with ``get``: a dict, or ADK's ``State`` (not a Mapping).
    """
    if state.get(learning_gate.LEARNING_LEVEL_LOCKED_STATE_KEY) is not True:
        return default
    stamped = state.get(learning_gate.LEARNING_LEVEL_STATE_KEY)
    if not isinstance(stamped, Mapping) or stamped.get("scenario_id") != scenario_id:
        return default
    level = stamped.get("level")
    if type(level) is not int or not 0 <= level <= REVEAL_LEVEL:
        return default
    return max(level, default)


def instruction_line(level: int) -> str:
    return (
        "Learning sandbox: this conversation is a practice exercise. The learner's "
        f"hint level is {level} of {REVEAL_LEVEL}. At this level, {LEVEL_RULES[level]}. "
        "Never state a value you have not been allowed to reveal."
    )


class DisclosureInstructionPlugin(BasePlugin):
    """Appends the learner's level to the root's system instruction.

    Never answers (always returns ``None``), so it is not a before-model
    short-circuit; ``test_learning_guard`` lists it as reviewed.
    """

    def __init__(
        self,
        *,
        root_agent_name: Callable[[], str],
        level_of: Callable[[CallbackContext], int],
    ) -> None:
        super().__init__(name="learning_disclosure_instruction")
        self._root = root_agent_name
        self._level_of = level_of

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        if callback_context.agent_name == self._root():
            llm_request.append_instructions([instruction_line(self._level_of(callback_context))])
        return None


def record_hint(subject: str, scenario_id: str, old: int, new: int) -> None:
    """The durable record a lab deducts hint costs from."""
    audit_event(
        "learning_hint",
        subject=subject,
        scenario_id=scenario_id,
        old_level=old,
        new_level=new,
    )
    _count_hint(new)


_HINTS: Any = None


def _count_hint(level: int) -> None:
    """``orrery_learning_hints_total{level}``, registered on first use."""
    global _HINTS
    if _HINTS is None:
        from prometheus_client import Counter

        _HINTS = Counter(
            "orrery_learning_hints_total",
            "Hints learners took, by the level reached",
            ["level"],
        )
    _HINTS.labels(level=str(level)).inc()
