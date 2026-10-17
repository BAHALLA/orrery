"""The batch route decides from what was actually checked, for any specialist.

The fallback once read a hand-kept list of the built-in systems' report keys,
so a third-party checker's "critical" was never read, and a sweep that
checked nothing inferred "healthy" from the silence.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from orrery_assistant import agent as chat


def _ctx(**state) -> MagicMock:
    ctx = MagicMock()
    ctx.state = dict(state)
    ctx.route = None
    return ctx


def test_the_report_keys_come_from_the_checkers():
    assert tuple(c.output_key for c in chat.HEALTH_CHECKERS) == chat._STATUS_KEYS


def test_a_third_party_checkers_report_is_read():
    state = {"redis_status": "STATUS: critical\n- no PONG from redis-0"}
    assert chat._infer_severity_from_status(state, ("redis_status",)) == "degraded"
    assert chat._infer_severity_from_status({"redis_status": "PONG"}, ("redis_status",)) == (
        "healthy"
    )


def test_silence_is_not_healthy():
    assert chat._infer_severity_from_status({}, ("redis_status",)) == "degraded"
    assert chat._infer_severity_from_status({}, ()) == "degraded"


def test_nothing_checked_reports_instead_of_remediating(monkeypatch):
    monkeypatch.setattr(chat, "_STATUS_KEYS", ())
    ctx = _ctx()
    assert chat.triage_route(ctx) == "resolved"  # no evidence, so no action
    assert ctx.state["no_systems_checked"] is True
    assert "no system was checked" in chat.final_report(ctx)


def test_no_remediating_specialist_hands_the_problem_to_a_human(monkeypatch):
    monkeypatch.setattr(chat, "_remediation", None)
    ctx = _ctx(incident_severity="critical")
    assert chat.triage_route(ctx) == "resolved"
    assert ctx.state["remediation_unavailable"] is True
    report = chat.final_report(ctx)
    assert "critical" in report and "a human must act" in report


def test_with_kubernetes_off_the_batch_run_has_nothing_to_act_with(monkeypatch):
    """The lab's configuration: switching k8s off takes its actions off the
    unattended path too, not only out of the sweep."""
    import importlib

    monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", "docker,k8s,elasticsearch")
    try:
        lab = importlib.reload(chat)
        nodes = {n.name for n in lab.orrery_triage_workflow.graph.nodes}
        assert lab._remediation is None
        assert not nodes & {"remediation_actor", "remediation_verifier", "remediation_summarizer"}
        assert {"kafka_health_checker", "observability_health_checker"} <= nodes
        assert "k8s_health_checker" not in nodes
    finally:
        monkeypatch.delenv("ORRERY_DISABLED_SPECIALISTS")
        importlib.reload(chat)
