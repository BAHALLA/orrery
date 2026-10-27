"""Orrery Studio P0c (AEP-031 §3): human approvals.

Runs go through the HTTP API with the stub model of the pattern tests. A run
pauses at a ``human_approval`` node until its owner decides through
``POST /studio/runs/{id}/approvals/{aid}``; the checks are who may decide,
on what, how often, and what happens to the run around the wait.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

pytest.importorskip("a2a")
pytest.importorskip("mcp")

from fastapi.testclient import TestClient  # noqa: E402

from orrery_core.studio.approval import digest_of  # noqa: E402
from orrery_core.studio.events import MAX_APPROVALS_PER_RUN  # noqa: E402
from orrery_core.studio.spec import SpecInvalid, ValidationContext, parse_spec  # noqa: E402
from orrery_core.studio.spec import validate_spec as check_spec  # noqa: E402

from .test_studio_patterns import (  # noqa: E402
    ADMIN,
    GraphStub,
    _bearer,
    entered,
    events,
    finish,
    make_app,
    node,
    spec_of,
    start,
)

BOB = _bearer("bob")


@pytest.fixture
def principals(monkeypatch):
    monkeypatch.setenv("ORRERY_HUMAN_CLIENTS", "orrery-console")


def gate(node_id: str = "gate", prompt: str = "Publish this draft?") -> dict[str, Any]:
    return {"id": node_id, "kind": "human_approval", "prompt": prompt}


GATED = spec_of(
    [node("writer"), gate(), node("publisher")],
    [
        {"from": "writer", "to": "gate"},
        {"from": "gate", "to": "publisher", "when": "approved"},
    ],
    "pipeline",
)

#: A human critic: a rejection goes back to the writer with the comment.
REVIEWED = spec_of(
    [node("writer"), gate(), node("publisher")],
    [
        {"from": "writer", "to": "gate"},
        {"from": "gate", "to": "publisher", "when": "approved"},
        {"from": "gate", "to": "writer", "when": "rejected", "max_iterations": 2},
    ],
    "critic_loop",
)


def pending(client: TestClient, run_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """The run's one pending approval, with its text, once it asks."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = client.get(f"/studio/runs/{run_id}/approvals", headers=ADMIN)
        assert res.status_code == 200, res.text
        if approvals := res.json()["approvals"]:
            [approval] = approvals
            return approval
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never asked for an approval")


def decide(
    client: TestClient, run_id: str, approval: dict[str, Any], decision: str, **body: Any
) -> Any:
    if decision == "approve":
        body.setdefault("digest", approval["digest"])
    return client.post(
        f"/studio/runs/{run_id}/approvals/{approval['approval_id']}",
        headers=body.pop("headers", ADMIN),
        json={"decision": decision, **body},
    )


@pytest.fixture
def stub() -> GraphStub:
    stub = GraphStub(script={}, calls=[], delay={}, hold={}, inputs={}, after={})
    stub.script["writer"] = lambda n, _t: f"draft {n}"
    return stub


# ── The decision ─────────────────────────────────────────────────────


def test_an_approved_run_goes_on_with_the_text_that_was_reviewed(principals, stub):
    with TestClient(make_app(stub)) as client:
        run_id = start(client, GATED)
        approval = pending(client, run_id)
        assert approval["node_id"] == "gate" and approval["kind"] == "approval"
        assert approval["prompt"] == "Publish this draft?"
        assert approval["text"] == "draft 1"
        assert approval["digest"] == digest_of("draft 1")
        summary = client.get(f"/studio/runs/{run_id}", headers=ADMIN).json()
        assert summary["status"] == "running"
        # The summary says what waits, never the text itself.
        [waiting] = summary["pending_approvals"]
        assert waiting["approval_id"] == approval["approval_id"] and "text" not in waiting

        res = decide(client, run_id, approval, "approve")
        assert res.status_code == 200 and res.json()["decision"] == "approved"
        summary = finish(client, run_id)
        trace = events(client, run_id)
    assert summary["status"] == "succeeded", summary
    assert summary["pending_approvals"] == []
    assert entered(trace) == ["writer", "gate", "publisher"]
    assert stub.inputs["publisher"] == ["draft 1"]
    requested = next(e for e in trace if e["type"] == "approval.requested")
    decided = next(e for e in trace if e["type"] == "approval.decided")
    assert requested["digest"] == digest_of("draft 1") and requested["size"] == len("draft 1")
    assert decided["decision"] == "approved" and decided["waited_ms"] >= 0
    # Content stays off the wire unless capture is on.
    assert "content" not in requested and "content" not in decided
    route = next(e for e in trace if e["type"] == "route.decision")
    assert route | {"label": "approved", "to": "publisher"} == route


def test_a_rejection_with_no_edge_ends_the_run_rejected(principals, stub):
    with TestClient(make_app(stub)) as client:
        run_id = start(client, GATED)
        approval = pending(client, run_id)
        res = decide(client, run_id, approval, "reject", comment="Not yet.")
        assert res.status_code == 200 and res.json()["decision"] == "rejected"
        summary = finish(client, run_id)
        trace = events(client, run_id)
    assert summary["status"] == "rejected"
    assert summary["error"] == "approval 'gate' was rejected"
    assert "publisher" not in entered(trace)
    exit_ = next(e for e in trace if e["type"] == "node.exit" and e["node_id"] == "gate")
    assert exit_["outcome"] == "rejected"
    assert trace[-1]["type"] == "run.end" and trace[-1]["outcome"] == "rejected"


def test_a_rejected_draft_goes_back_with_the_reviewers_comment(principals, stub):
    with TestClient(make_app(stub)) as client:
        run_id = start(client, REVIEWED)
        first = pending(client, run_id)
        assert (
            decide(client, run_id, first, "reject", comment="Shorter, please.").status_code == 200
        )
        second = pending(client, run_id)
        assert second["approval_id"] != first["approval_id"]
        assert second["text"] == "draft 2"
        assert decide(client, run_id, second, "approve").status_code == 200
        summary = finish(client, run_id)
    assert summary["status"] == "succeeded", summary
    assert "Shorter, please." in stub.inputs["writer"][1]
    assert "draft 1" in stub.inputs["writer"][1]
    assert stub.inputs["publisher"] == ["draft 2"]


def test_with_capture_on_the_events_carry_the_text_and_the_comment(principals, stub):
    with TestClient(make_app(stub, capture_content=True)) as client:
        run_id = start(client, GATED)
        approval = pending(client, run_id)
        decide(client, run_id, approval, "reject", comment="Not yet.")
        finish(client, run_id)
        trace = events(client, run_id)
    requested = next(e for e in trace if e["type"] == "approval.requested")
    decided = next(e for e in trace if e["type"] == "approval.decided")
    assert requested["content"] == "draft 1" and decided["content"] == "Not yet."


def test_the_text_under_review_is_redacted_but_the_digest_is_of_what_runs_on(principals, stub):
    stub.script["writer"] = lambda _n, _t: "Mail jane.doe@example.com the draft."
    with TestClient(make_app(stub)) as client:
        run_id = start(client, GATED)
        approval = pending(client, run_id)
        assert "jane.doe@example.com" not in approval["text"]
        assert approval["digest"] == digest_of("Mail jane.doe@example.com the draft.")
        decide(client, run_id, approval, "approve")
        finish(client, run_id)
    assert stub.inputs["publisher"] == ["Mail jane.doe@example.com the draft."]


# ── Who decides, on what, how often ──────────────────────────────────


def test_approving_names_the_digest_of_the_text_seen(principals, stub):
    with TestClient(make_app(stub)) as client:
        run_id = start(client, GATED)
        approval = pending(client, run_id)
        url = f"/studio/runs/{run_id}/approvals/{approval['approval_id']}"
        missing = client.post(url, headers=ADMIN, json={"decision": "approve"})
        assert missing.status_code == 422
        wrong = decide(client, run_id, approval, "approve", digest="0" * 64)
        assert wrong.status_code == 409 and "digest" in wrong.json()["detail"]
        # Still pending: the run waits for a real decision.
        assert pending(client, run_id)["approval_id"] == approval["approval_id"]
        assert decide(client, run_id, approval, "approve").status_code == 200
        assert finish(client, run_id)["status"] == "succeeded"


def test_an_approval_is_decided_once(principals, stub):
    with TestClient(make_app(stub)) as client:
        run_id = start(client, GATED)
        approval = pending(client, run_id)
        assert decide(client, run_id, approval, "reject").status_code == 200
        again = decide(client, run_id, approval, "approve")
        finish(client, run_id)
    assert again.status_code in (404, 409)


def test_only_the_runs_owner_decides(principals, stub):
    with TestClient(make_app(stub)) as client:
        run_id = start(client, GATED)
        approval = pending(client, run_id)
        # Another admin does not see the run at all.
        assert client.get(f"/studio/runs/{run_id}/approvals", headers=BOB).status_code == 404
        assert decide(client, run_id, approval, "approve", headers=BOB).status_code == 404
        # A viewer is not a Studio user.
        viewer = _bearer("carol", ("viewer",))
        assert decide(client, run_id, approval, "approve", headers=viewer).status_code == 403
        unknown = client.post(
            f"/studio/runs/{run_id}/approvals/{'f' * 32}",
            headers=ADMIN,
            json={"decision": "reject"},
        )
        assert unknown.status_code == 404
        decide(client, run_id, approval, "reject")
        finish(client, run_id)


# ── Around the wait ──────────────────────────────────────────────────


def test_an_undecided_approval_expires_as_a_reject(principals, stub):
    with TestClient(make_app(stub, approval_timeout_s=1)) as client:
        run_id = start(client, REVIEWED)
        pending(client, run_id)
        summary = finish(client, run_id, timeout=10)
        trace = events(client, run_id)
    # An expiry ends the run: it never goes round the loop to ask again.
    assert summary["status"] == "rejected"
    assert summary["error"] == "approval 'gate' expired"
    decided = next(e for e in trace if e["type"] == "approval.decided")
    assert decided["decision"] == "expired"
    assert len(stub.inputs["writer"]) == 1


def test_waiting_for_a_person_does_not_spend_the_runs_deadline(principals, stub):
    spec = GATED | {"budgets": {"timeout_s": 1}}
    with TestClient(make_app(stub)) as client:
        run_id = start(client, spec)
        approval = pending(client, run_id)
        time.sleep(1.5)  # past the run's own deadline, while it waits
        assert decide(client, run_id, approval, "approve").status_code == 200
        summary = finish(client, run_id)
    assert summary["status"] == "succeeded", summary
    assert summary["totals"]["wall_ms"] >= 1500


def test_the_deadline_still_holds_outside_the_wait(principals, stub):
    stub.delay["publisher"] = 3.0
    spec = GATED | {"budgets": {"timeout_s": 1}}
    with TestClient(make_app(stub)) as client:
        run_id = start(client, spec)
        decide(client, run_id, pending(client, run_id), "approve")
        summary = finish(client, run_id, timeout=10)
        trace = events(client, run_id)
    assert summary["status"] == "budget_exhausted"
    [hit] = [e for e in trace if e["type"] == "budget.hit"]
    assert hit["budget"] == "timeout_s"


def test_cancelling_a_waiting_run_cancels_its_approval(principals, stub):
    with TestClient(make_app(stub)) as client:
        run_id = start(client, GATED)
        approval = pending(client, run_id)
        assert client.post(f"/studio/runs/{run_id}/cancel", headers=ADMIN).status_code == 200
        summary = finish(client, run_id)
        trace = events(client, run_id)
        late = decide(client, run_id, approval, "approve")
    assert summary["status"] == "cancelled"
    decided = next(e for e in trace if e["type"] == "approval.decided")
    assert decided["decision"] == "cancelled"
    assert late.status_code in (404, 409)


def test_a_run_asks_for_a_bounded_number_of_approvals(principals, stub):
    # Three gates round a loop of 5: up to 18 asks, past the cap of 12.
    spec = spec_of(
        [node("writer"), gate("first"), gate("second"), gate("third")],
        [
            {"from": "writer", "to": "first"},
            {"from": "first", "to": "second", "when": "approved"},
            {"from": "second", "to": "third", "when": "approved"},
            {"from": "third", "to": "writer", "when": "rejected", "max_iterations": 5},
        ],
        "critic_loop",
    )
    with TestClient(make_app(stub)) as client:
        run_id = start(client, spec)
        for _ in range(MAX_APPROVALS_PER_RUN):
            summary = client.get(f"/studio/runs/{run_id}", headers=ADMIN).json()
            if summary["status"] != "running":
                break
            approval = pending(client, run_id)
            verdict = "reject" if approval["node_id"] == "third" else "approve"
            assert decide(client, run_id, approval, verdict).status_code == 200
        summary = finish(client, run_id)
    assert summary["status"] == "failed"
    assert f"more than {MAX_APPROVALS_PER_RUN} approvals" in summary["error"]


# ── The spec ─────────────────────────────────────────────────────────


def _errors(raw: dict[str, Any]) -> list[str]:
    try:
        spec = parse_spec(raw)
    except SpecInvalid as exc:
        return [e.msg for e in exc.errors]
    return [e.msg for e in check_spec(spec, ValidationContext())]


def test_a_pipeline_may_hold_an_approval():
    assert _errors(GATED) == []


def test_an_approvals_edges_are_taken_on_approved_or_rejected():
    spec = spec_of(
        [node("writer"), gate(), node("publisher")],
        [{"from": "writer", "to": "gate"}, {"from": "gate", "to": "publisher"}],
        "pipeline",
    )
    [error] = _errors(spec)
    assert "each of its edges needs when" in error and "approved" in error


def test_an_approval_does_not_sit_on_a_parallel_branch():
    spec = spec_of(
        [
            {"id": "fan", "kind": "parallel"},
            node("a"),
            gate(),
            {"id": "meet", "kind": "join"},
            node("after"),
        ],
        [
            {"from": "fan", "to": "a"},
            {"from": "fan", "to": "gate"},
            {"from": "a", "to": "meet"},
            {"from": "gate", "to": "meet", "when": "approved"},
            {"from": "meet", "to": "after"},
        ],
        "fan_out_fan_in",
    )
    assert any("waits after the join" in e for e in _errors(spec))


def test_a_router_does_not_follow_an_approval():
    spec = spec_of(
        [node("writer"), gate(), {"id": "pick", "kind": "router"}, node("publisher")],
        [
            {"from": "writer", "to": "gate"},
            {"from": "gate", "to": "pick"},
            {"from": "pick", "to": "publisher", "when": "approved"},
        ],
        "router",
    )
    assert any("cannot follow human approval" in e for e in _errors(spec))
