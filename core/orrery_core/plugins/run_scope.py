"""Per-run bookkeeping shared by the budget plugins.

Every budget in this package is charged per **run**: one agent's work within
one ADK invocation, keyed ``<invocation id>/<agent name>`` (:func:`run_key`).
Not per session or per process. It is the unit the cost grows in: each model
call in a run re-sends the run so far, so a run's cost grows with the square of
its length, and the context a run can overflow is that agent's own.

Both halves of the key matter. An ``AgentTool`` specialist gets its own
invocation, so its runaway loop is bounded inside the specialist and the root's
turn keeps a separate allowance. The agent name is what separates the nodes of
a graph ``Workflow``: ADK 2.0 runs every node under the *parent's* invocation
id, so without it the five parallel health checkers, the summarizer and the
remediation loop of the triage sweep would all draw on one budget.

The ledgers are LRU maps rather than session state: they hold an id and a few
integers per run, are read on every tool call, and must not leak into the
session's persisted state deltas. A process serving many conversations evicts
the least-recently-charged run, which can only *reset* a budget, never let one
run's count reach another.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import Any

#: Runs remembered at once by each ledger. A run is evicted only once this many
#: others have charged it since its own last charge.
DEFAULT_LEDGER_SIZE = 512


def invocation_id(context: Any) -> str:
    """The run's invocation id, or ``""`` when it cannot be determined.

    Read from the context's property first, then from the invocation context
    behind it. An empty id disables the budget for that call, so it is worth
    both chances.
    """
    value = getattr(context, "invocation_id", None)
    if not value:
        inner = getattr(context, "_invocation_context", None)
        value = getattr(inner, "invocation_id", None)
    return str(value) if isinstance(value, str) and value else ""


def run_key(context: Any) -> str:
    """``<invocation id>/<agent name>`` for a tool or callback context, or ``""``.

    ``""`` (no invocation id) disables the budgets for that call rather than
    pooling every unidentifiable call into one shared count.
    """
    run = invocation_id(context)
    if not run:
        return ""
    agent = getattr(context, "agent_name", None)
    return f"{run}/{agent}" if isinstance(agent, str) and agent else run


def session_id(context: Any) -> str:
    """The session id behind a tool or callback context, or ``""``."""
    session = getattr(context, "session", None)
    if session is None:
        inner = getattr(context, "_invocation_context", None)
        session = getattr(inner, "session", None)
    value = getattr(session, "id", None)
    return str(value) if isinstance(value, str) and value else ""


class RunLedger[V]:
    """A bounded ``key -> V`` map that creates entries on first use (LRU)."""

    def __init__(self, factory: Callable[[], V], size: int = DEFAULT_LEDGER_SIZE) -> None:
        self._factory = factory
        self._size = size
        self._entries: OrderedDict[str, V] = OrderedDict()

    def get(self, key: str) -> V:
        """Return the entry for *key*, creating it, and mark it recently used."""
        entry = self._entries.get(key)
        if entry is None:
            entry = self._factory()
            self._entries[key] = entry
            while len(self._entries) > self._size:
                self._entries.popitem(last=False)
        else:
            self._entries.move_to_end(key)
        return entry

    def peek(self, key: str) -> V | None:
        """Return the entry for *key* without creating or touching it."""
        return self._entries.get(key)

    def pop(self, key: str) -> None:
        self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)
