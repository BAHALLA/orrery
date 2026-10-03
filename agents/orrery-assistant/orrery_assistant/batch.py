"""Judge an unattended triage sweep: did it cover what it says it covered?

A sweep run from cron has no human reading it as it happens, so its exit code
and its last log line are the whole of what an operator (or an alert on the
CronJob) gets. Two outcomes used to be indistinguishable from a healthy sweep,
because both printed a report and exited ``0``:

* **A sweep that was refused its reads.** RBAC, the autonomy level or a run
  budget answers instead of the tool; the checker reports on what it could see;
  the summarizer writes "all systems healthy" over a system nobody looked at.
  A refusal is not an all-clear, and the report's own prose need not admit the
  hole — it is often written by a model that was never told.
* **A sweep that produced no verdict at all.** The run raised, or the
  summarizer never called ``record_triage_verdict`` and the route had to infer
  one. Silence looked exactly like "nothing to report".

Refusals have to be read from a :class:`~orrery_core.ToolLedger`, not from the
events the Runner yields: the health checks run inside graph nodes and
``AgentTool`` specialists whose tool results never reach the caller's stream.

Not every refusal is a hole. A remediation step stopped at the confirmation
gate, or a destructive one RBAC refuses the batch role, is the unattended path
working as designed — the action is reported as awaiting a human, and the sweep
is still complete.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any

from orrery_core import ToolLedger
from orrery_core.plugins import (
    CALL_BUDGET_STATUS,
    DELEGATION_REFUSED_STATUS,
    REPEAT_REFUSED_STATUS,
)

#: Refusals that cut a sweep short whatever the tool — the run ran out of room.
_BUDGET_STATUSES = frozenset({CALL_BUDGET_STATUS, DELEGATION_REFUSED_STATUS, REPEAT_REFUSED_STATUS})


class SweepExit(IntEnum):
    """Process exit codes for ``run_triage.py``. Alert on anything non-zero."""

    #: Completed with full coverage — whatever the severity it found.
    COMPLETE = 0
    #: Raised, or produced no verdict: there is no result to trust.
    FAILED = 1
    #: Completed, but refused something it needed, or the verdict was inferred:
    #: the report may describe less than it appears to.
    INCOMPLETE = 2


@dataclass
class SweepOutcome:
    """What one sweep did, in a form a log line and an exit code can carry."""

    severity: str | None = None
    verdict_missing: bool = False
    error: str | None = None
    calls: int = 0
    #: ``(tool, status)`` refusals that left a hole in the sweep's coverage.
    coverage_holes: list[tuple[str, str]] = field(default_factory=list)
    #: Guarded actions the sweep wanted and stopped at a human's gate.
    awaiting_human: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> SweepExit:
        if self.error is not None or self.severity is None:
            return SweepExit.FAILED
        if self.coverage_holes or self.verdict_missing:
            return SweepExit.INCOMPLETE
        return SweepExit.COMPLETE

    def summary(self) -> str:
        """One JSON line for the log: the record a CronJob alert links to."""
        payload: dict[str, Any] = asdict(self)
        payload["outcome"] = self.exit_code.name.lower()
        payload["exit_code"] = int(self.exit_code)
        return json.dumps(payload, sort_keys=True)


def judge_sweep(
    state: dict[str, Any], ledger: ToolLedger, error: BaseException | None = None
) -> SweepOutcome:
    """Build the :class:`SweepOutcome` for a finished (or failed) sweep.

    Args:
        state: The session state after the run.
        ledger: The ledger armed for the run.
        error: The exception the run raised, if it raised.
    """
    holes: list[tuple[str, str]] = []
    awaiting: list[str] = []
    for entry in ledger.entries:
        if not entry.refused:
            continue
        if entry.status in _BUDGET_STATUSES or not entry.guarded:
            # A read refused, or the run out of budget: something went unchecked.
            holes.append((entry.tool, entry.status))
        else:
            # A guarded action stopped at a gate: the unattended path by design.
            awaiting.append(entry.tool)

    severity = state.get("incident_severity")
    return SweepOutcome(
        severity=str(severity) if severity else None,
        verdict_missing=bool(state.get("triage_verdict_missing")),
        error=f"{type(error).__name__}: {error}" if error is not None else None,
        calls=len(ledger.calls()),
        coverage_holes=holes,
        awaiting_human=list(dict.fromkeys(awaiting)),
    )
