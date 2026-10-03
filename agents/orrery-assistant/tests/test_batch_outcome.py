"""Tests for judging an unattended triage sweep (exit code + summary line)."""

from __future__ import annotations

import json

from orrery_assistant.batch import SweepExit, judge_sweep
from orrery_core import ToolLedger
from orrery_core.plugins import CALL_BUDGET_STATUS, LedgerEntry


def _ledger(*entries: tuple[str, str, bool, bool]) -> ToolLedger:
    ledger = ToolLedger()
    for tool, status, guarded, refused in entries:
        ledger.entries.append(
            LedgerEntry(tool=tool, status=status, guarded=guarded, refused=refused)
        )
    return ledger


def test_a_clean_sweep_is_complete_whatever_it_found():
    ledger = _ledger(
        ("list_pods", "success", False, False), ("get_cluster_health", "", False, False)
    )
    outcome = judge_sweep({"incident_severity": "critical"}, ledger)
    assert outcome.exit_code is SweepExit.COMPLETE
    assert outcome.calls == 2


def test_a_refused_read_is_a_hole_not_an_all_clear():
    ledger = _ledger(
        ("list_pods", "access_denied", False, True),
        ("get_cluster_health", "success", False, False),
    )
    outcome = judge_sweep({"incident_severity": "healthy"}, ledger)
    assert outcome.exit_code is SweepExit.INCOMPLETE
    assert outcome.coverage_holes == [("list_pods", "access_denied")]


def test_a_spent_budget_is_a_hole_even_on_a_read():
    ledger = _ledger(("search_logs", CALL_BUDGET_STATUS, False, True))
    outcome = judge_sweep({"incident_severity": "degraded"}, ledger)
    assert outcome.exit_code is SweepExit.INCOMPLETE


def test_a_gated_remediation_is_awaiting_a_human_not_a_hole():
    """The unattended path stopping at the confirmation gate is working as designed."""
    ledger = _ledger(
        ("scale_deployment", "confirmation_required", True, True),
        ("restart_deployment", "access_denied", True, True),
        ("scale_deployment", "confirmation_required", True, True),
    )
    outcome = judge_sweep({"incident_severity": "critical"}, ledger)
    assert outcome.exit_code is SweepExit.COMPLETE
    assert outcome.awaiting_human == ["scale_deployment", "restart_deployment"]
    assert outcome.coverage_holes == []


def test_an_inferred_verdict_is_incomplete():
    outcome = judge_sweep(
        {"incident_severity": "degraded", "triage_verdict_missing": True}, ToolLedger()
    )
    assert outcome.exit_code is SweepExit.INCOMPLETE


def test_silence_and_errors_fail():
    assert judge_sweep({}, ToolLedger()).exit_code is SweepExit.FAILED
    raised = judge_sweep({"incident_severity": "healthy"}, ToolLedger(), RuntimeError("boom"))
    assert raised.exit_code is SweepExit.FAILED
    assert raised.error == "RuntimeError: boom"


def test_summary_is_one_json_line_with_the_outcome():
    ledger = _ledger(("list_pods", "access_denied", False, True))
    line = judge_sweep({"incident_severity": "healthy"}, ledger).summary()
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["outcome"] == "incomplete"
    assert payload["exit_code"] == 2
    assert payload["coverage_holes"] == [["list_pods", "access_denied"]]
