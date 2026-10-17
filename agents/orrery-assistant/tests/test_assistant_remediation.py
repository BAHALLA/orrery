"""Unit tests for the graph-based remediation nodes (ADR-003)."""

from unittest.mock import MagicMock

import pytest

# Importing agent validates that the full graph module loads without errors
from orrery_assistant.agent import _remediation
from orrery_assistant.remediation import (
    MAX_REMEDIATION_ITERATIONS,
    mark_remediation_resolved,
    verify_route,
)

# Built from the k8s specialist's declared Remediation (on in the test env).
assert _remediation is not None
remediation_actor = _remediation.actor
remediation_verifier = _remediation.verifier
remediation_summarizer = _remediation.summarizer


def _Ctx(state: dict | None = None) -> MagicMock:
    """Minimal Context double exposing mutable state + a settable route."""
    ctx = MagicMock()
    ctx.state = state or {}
    ctx.route = None
    return ctx


# ── mark_remediation_resolved tool ───────────────────────────────────


class TestMarkRemediationResolved:
    @pytest.mark.asyncio
    async def test_sets_resolved_flag_in_state(self):
        ctx = MagicMock()
        ctx.state = {}
        result = await mark_remediation_resolved("issue resolved", tool_context=ctx)
        assert ctx.state["remediation_resolved"] is True
        assert ctx.state["remediation_resolution_reason"] == "issue resolved"
        assert result["status"] == "remediation_complete"
        assert result["reason"] == "issue resolved"


# ── verify_route loop logic (replaces LoopAgent + exit_loop) ─────────


class TestVerifyRoute:
    def test_routes_done_when_resolved(self):
        ctx = _Ctx({"remediation_resolved": True})
        assert verify_route(ctx) == "done"
        assert ctx.route == "done"

    def test_routes_retry_when_unresolved_under_cap(self):
        ctx = _Ctx({"remediation_resolved": False, "remediation_iteration": 0})
        assert verify_route(ctx) == "retry"
        assert ctx.state["remediation_iteration"] == 1

    def test_increments_iteration_each_call(self):
        ctx = _Ctx({"remediation_iteration": 0})
        verify_route(ctx)
        verify_route(ctx)
        assert ctx.state["remediation_iteration"] == 2

    def test_routes_done_at_iteration_cap(self):
        ctx = _Ctx({"remediation_iteration": MAX_REMEDIATION_ITERATIONS - 1})
        assert verify_route(ctx) == "done"


# ── Node wiring ──────────────────────────────────────────────────────


class TestRemediationNodeWiring:
    def test_remediation_actor_has_tools(self):
        tool_names = {
            getattr(t, "name", getattr(t, "__name__", None)) for t in remediation_actor.tools
        }
        assert {"restart_deployment", "scale_deployment", "rollback_deployment"} <= tool_names

    def test_remediation_actor_has_output_key(self):
        assert remediation_actor.output_key == "remediation_action"

    def test_remediation_verifier_signals_via_resolve_tool(self):
        tool_names = {
            getattr(t, "name", getattr(t, "__name__", None)) for t in remediation_verifier.tools
        }
        assert "mark_remediation_resolved" in tool_names
        assert "exit_loop" not in tool_names

    def test_remediation_verifier_has_output_key(self):
        assert remediation_verifier.output_key == "verification_result"

    def test_remediation_summarizer_has_output_key(self):
        assert remediation_summarizer.output_key == "remediation_summary"
