"""Structural tests for the graph root + planner attachment (ADR-003).

These are deterministic, LLM-free checks that verify the Workflow graph is
wired correctly, the deterministic routing functions behave, and the right
reasoning agents pick up a planner when ``ORRERY_PLANNER`` is set while
tool-leaf agents stay planner-free. They complement the per-planner unit
tests in ``core/tests/test_base.py::TestResolvePlanner`` and the agent-level
evals in ``test_orrery_eval.py``.

The agent module reads ``ORRERY_PLANNER`` once at import time, so each
test reloads the module after setting the env var.

The root is now a ``Workflow`` (not an ``LlmAgent``), so it has no planner;
planner assertions target the reasoning nodes (triage_summarizer,
remediation_actor).
"""

import importlib
import sys
from unittest.mock import MagicMock

import pytest
from google.adk import Workflow
from google.adk.planners import BuiltInPlanner, PlanReActPlanner


def _reload_agent():
    """Reload the agent + remediation modules so module-level
    ``resolve_planner()`` re-runs with the current env."""
    for name in (
        "orrery_assistant.remediation",
        "orrery_assistant.agent",
    ):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    return (
        sys.modules["orrery_assistant.agent"],
        sys.modules["orrery_assistant.remediation"],
    )


@pytest.fixture
def clean_planner_env(monkeypatch):
    """Pin ORRERY_PLANNER to ``none`` so a developer-set value in the
    root .env does not leak through ``load_agent_env()`` on reload.

    Tests that need a different value override via ``setenv``."""
    monkeypatch.setenv("ORRERY_PLANNER", "none")
    for var in (
        "ORRERY_PLANNER_THINKING_BUDGET",
        "ORRERY_PLANNER_INCLUDE_THOUGHTS",
        "MODEL_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)


class TestPlannerWiring:
    def test_default_no_planner(self, clean_planner_env):
        agent_mod, rem_mod = _reload_agent()
        # The reasoning agents that *can* opt in start planner-free when
        # ORRERY_PLANNER is unset. Zero behavior change.
        assert agent_mod.orrery_chat_agent.planner is None
        assert agent_mod.triage_summarizer.planner is None
        assert agent_mod._remediation.actor.planner is None

    def test_plan_react_attaches_to_reasoning_agents(self, clean_planner_env, monkeypatch):
        monkeypatch.setenv("ORRERY_PLANNER", "plan_react")
        agent_mod, rem_mod = _reload_agent()

        # The reasoning-heavy orchestrator/synthesis/actor agents opt in.
        assert isinstance(agent_mod.orrery_chat_agent.planner, PlanReActPlanner)
        assert isinstance(agent_mod.triage_summarizer.planner, PlanReActPlanner)
        assert isinstance(agent_mod._remediation.actor.planner, PlanReActPlanner)

    def test_leaves_stay_planner_free_under_plan_react(self, clean_planner_env, monkeypatch):
        """Per-system health checkers, the journal writer, and the
        remediation verifier do one short tool sequence per turn — adding
        a planner there would burn latency without changing the output."""
        monkeypatch.setenv("ORRERY_PLANNER", "plan_react")
        agent_mod, rem_mod = _reload_agent()

        # One checker per specialist that declares a HealthCheck.
        assert {c.name for c in agent_mod.HEALTH_CHECKERS} == {
            "kafka_health_checker",
            "k8s_health_checker",
            "docker_health_checker",
            "observability_health_checker",
            "elasticsearch_health_checker",
        }
        leaf_agents = [
            *agent_mod.HEALTH_CHECKERS,
            agent_mod.journal_writer,
            agent_mod._remediation.verifier,
        ]
        for leaf in leaf_agents:
            assert leaf.planner is None, (
                f"{leaf.name} should stay planner-free; planning belongs on "
                f"orchestration / synthesis agents, not on tool-leaf agents."
            )

    def test_builtin_falls_back_for_non_gemini(self, clean_planner_env, monkeypatch, caplog):
        """builtin requires Gemini; any other provider falls back to no
        planner with a warning so LiteLLM-routed deployments are silent."""
        monkeypatch.setenv("ORRERY_PLANNER", "builtin")
        monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
        monkeypatch.setenv("MODEL_NAME", "anthropic/claude-sonnet-4-20250514")
        with caplog.at_level("WARNING", logger="orrery.base"):
            agent_mod, rem_mod = _reload_agent()
        assert agent_mod.triage_summarizer.planner is None
        assert agent_mod._remediation.actor.planner is None

    def test_builtin_attaches_under_gemini(self, clean_planner_env, monkeypatch):
        monkeypatch.setenv("ORRERY_PLANNER", "builtin")
        # MODEL_PROVIDER unset defaults to gemini in resolve_planner.
        agent_mod, rem_mod = _reload_agent()
        assert isinstance(agent_mod.triage_summarizer.planner, BuiltInPlanner)
        assert isinstance(agent_mod._remediation.actor.planner, BuiltInPlanner)


# ── Graph structure + deterministic routing (ADR-003) ────────────────


class TestRootAndGraphStructure:
    def test_root_is_conversational_chat_agent(self):
        agent_mod, _ = _reload_agent()
        # The interactive root is the chat coordinator, NOT the Workflow (ADK
        # forbids a chat-mode agent as a routed graph node).
        assert agent_mod.root_agent is agent_mod.orrery_chat_agent
        assert not isinstance(agent_mod.root_agent, Workflow)

    def test_triage_workflow_is_separate_graph(self):
        agent_mod, _ = _reload_agent()
        assert isinstance(agent_mod.orrery_triage_workflow, Workflow)
        node_names = {n.name for n in agent_mod.orrery_triage_workflow.graph.nodes}
        expected = {
            "kafka_health_checker",
            "k8s_health_checker",
            "docker_health_checker",
            "observability_health_checker",
            "elasticsearch_health_checker",
            "health_join",
            "triage_summarizer",
            "journal_writer",
            "triage_route",
            "remediation_actor",
            "remediation_verifier",
            "verify_route",
            "remediation_summarizer",
            "final_report",
        }
        assert expected <= node_names
        # The chat coordinator must NOT be a node in the deterministic graph.
        assert "orrery_chat_agent" not in node_names

    def test_coordinator_has_specialists_and_triage_tools(self):
        agent_mod, _ = _reload_agent()
        tool_names = {
            getattr(getattr(t, "agent", t), "name", getattr(t, "name", None))
            for t in agent_mod.orrery_chat_agent.tools
        }
        for specialist in (
            "kafka_health_agent",
            "k8s_health_agent",
            "observability_agent",
            "elasticsearch_agent",
            "docker_agent",
            "ops_journal_agent",
            "incident_triage_agent",  # broad-sweep delegation tool
        ):
            assert specialist in tool_names


class TestTriageRouting:
    def test_record_triage_verdict_normalizes_and_stores(self):
        agent_mod, _ = _reload_agent()
        import asyncio

        ctx = MagicMock()
        ctx.state = {}
        result = asyncio.run(
            agent_mod.record_triage_verdict("CRITICAL", "report body", tool_context=ctx)
        )
        assert ctx.state["incident_severity"] == "critical"
        assert ctx.state["triage_report"] == "report body"
        assert result["overall_status"] == "critical"

    def test_record_triage_verdict_unknown_defaults_to_degraded(self):
        agent_mod, _ = _reload_agent()
        import asyncio

        ctx = MagicMock()
        ctx.state = {}
        asyncio.run(agent_mod.record_triage_verdict("bogus", "r", tool_context=ctx))
        assert ctx.state["incident_severity"] == "degraded"

    def test_triage_route_remediates_on_issues(self):
        agent_mod, _ = _reload_agent()
        ctx = MagicMock()
        ctx.state = {"incident_severity": "critical"}
        ctx.route = None
        assert agent_mod.triage_route(ctx) == "remediate"
        assert ctx.state["remediation_iteration"] == 0
        assert ctx.state["remediation_resolved"] is False

    def test_triage_route_resolves_when_healthy(self):
        agent_mod, _ = _reload_agent()
        ctx = MagicMock()
        ctx.state = {"incident_severity": "healthy"}
        ctx.route = None
        assert agent_mod.triage_route(ctx) == "resolved"

    def test_missing_verdict_infers_from_status_and_flags(self):
        """No structured verdict + a problem signal in a status report must
        not silently resolve — it infers degraded and sets the flag."""
        agent_mod, _ = _reload_agent()
        ctx = MagicMock()
        ctx.state = {"k8s_status": "pod web-1 is in CrashLoopBackOff"}
        ctx.route = None
        assert agent_mod.triage_route(ctx) == "remediate"
        assert ctx.state["triage_verdict_missing"] is True
        assert ctx.state["incident_severity"] == "degraded"

    def test_missing_verdict_clean_status_resolves_but_flags(self):
        agent_mod, _ = _reload_agent()
        ctx = MagicMock()
        ctx.state = {"k8s_status": "all nodes ready", "kafka_status": "2 brokers online"}
        ctx.route = None
        assert agent_mod.triage_route(ctx) == "resolved"
        assert ctx.state["triage_verdict_missing"] is True

    def test_final_report_surfaces_missing_verdict_caveat(self):
        agent_mod, _ = _reload_agent()
        ctx = MagicMock()
        ctx.state = {"incident_severity": "healthy", "triage_verdict_missing": True}
        assert "manual review" in agent_mod.final_report(ctx).lower()

    def test_final_report_prefers_remediation_summary(self):
        agent_mod, _ = _reload_agent()
        ctx = MagicMock()
        ctx.state = {"remediation_summary": "fixed it"}
        assert agent_mod.final_report(ctx) == "fixed it"

    def test_final_report_falls_back_to_severity(self):
        agent_mod, _ = _reload_agent()
        ctx = MagicMock()
        ctx.state = {"incident_severity": "healthy"}
        assert "healthy" in agent_mod.final_report(ctx)


class TestSweepFollowsEnabledSpecialists:
    """ORRERY_DISABLED_SPECIALISTS reaches both sweeps, not just the routing."""

    def test_env_disabled_specialists_are_not_swept(self, clean_planner_env, monkeypatch):
        monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", "docker,k8s,elasticsearch")
        try:
            agent_mod, _ = _reload_agent()
            # Batch workflow: only the enabled systems' checkers fan out.
            assert {c.name for c in agent_mod.HEALTH_CHECKERS} == {
                "kafka_health_checker",
                "observability_health_checker",
            }
            node_names = {n.name for n in agent_mod.orrery_triage_workflow.graph.nodes}
            assert "docker_health_checker" not in node_names
            assert "k8s_health_checker" not in node_names
            # Conversational sweep: no tool of a disabled specialist.
            tools = {
                getattr(t, "name", None) or getattr(t, "__name__", "")
                for t in agent_mod.incident_triage_agent.tools
            }
            assert "list_containers" not in tools
            assert "list_pods" not in tools
            assert "es_get_cluster_health" not in tools
            assert "get_consumer_lag" in tools
        finally:
            monkeypatch.delenv("ORRERY_DISABLED_SPECIALISTS")
            _reload_agent()
