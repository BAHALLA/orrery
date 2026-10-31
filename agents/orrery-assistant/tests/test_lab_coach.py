"""The learning sandbox's coach (AEP-032 §5): where it comes from and what it holds.

It is built from the active specialists' read-only tools, only in an
activated process, and its name is reserved so no declarative agent can take
it. A guarded tool in its list fails here: a confirmation gate on a coach
whose design is that learners cannot change the lab would be a bug.
"""

from __future__ import annotations

import pytest

from orrery_assistant.agent import RESERVED_AGENT_NAMES
from orrery_core.agent.declarative import (
    DeclarativeAgentConfig,
    DeclarativeAgentsFile,
    build_declarative_agents,
)
from orrery_core.learning import coach as coach_module
from orrery_core.learning.coach import COACH_NAME, COACH_TOOLS, build_coach
from orrery_core.registry import discover_specialists
from orrery_core.security.guardrails import get_guard_level, is_confirmation_gate


def _specialists(monkeypatch, disabled: str):
    monkeypatch.delenv("ORRERY_SPECIALISTS", raising=False)
    monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", disabled)
    return discover_specialists(record=False)


def test_builds_with_the_lab_specialist_set(monkeypatch):
    coach = build_coach(_specialists(monkeypatch, "docker,k8s,elasticsearch"))
    assert coach is not None and coach.name == COACH_NAME


def test_builds_with_every_specialist(monkeypatch):
    coach = build_coach(_specialists(monkeypatch, ""))
    assert coach is not None
    names = {t.__name__ for t in coach.tools}
    # Every tool resolves; only Loki's depends on LOKI_URL being set.
    assert names <= set(COACH_TOOLS)
    assert set(COACH_TOOLS) - names <= {"query_loki_logs"}


def test_it_teaches_only_what_it_can_run():
    from orrery_core.learning.coach import practice_examples

    assert practice_examples({"get_consumer_lag"}) == "a kafka-consumer-groups invocation"
    # PromQL is taught from either Prometheus query tool.
    assert practice_examples({"query_prometheus_range"}) == "a PromQL query"
    assert "LogQL" in practice_examples({"query_loki_logs"})
    assert "kafka" not in practice_examples({"query_prometheus", "query_loki_logs"})
    assert practice_examples(set()) == "the equivalent command line or query"


def test_the_instruction_has_no_placeholder_left(monkeypatch):
    from orrery_core.agent.base import base_instruction

    coach = build_coach(_specialists(monkeypatch, "docker,k8s,elasticsearch,kafka"))
    assert coach is not None
    text = base_instruction(coach)
    assert "{practice}" not in text and "kafka-consumer-groups" not in text


def test_level_three_may_reveal():
    assert "Below level 3, never name the real" in coach_module.COACH_INSTRUCTION
    assert "at level 3, give the" in coach_module.COACH_INSTRUCTION


def test_is_read_only(monkeypatch):
    coach = build_coach(_specialists(monkeypatch, "docker,k8s,elasticsearch"))
    assert coach is not None
    assert {t.__name__: lvl for t in coach.tools if (lvl := get_guard_level(t))} == {}
    assert not is_confirmation_gate(coach.before_tool_callback)


def test_a_missing_tool_is_skipped_not_fatal(monkeypatch):
    coach = build_coach(_specialists(monkeypatch, "docker,k8s,elasticsearch,observability"))
    assert coach is not None
    names = {t.__name__ for t in coach.tools}
    assert "get_consumer_lag" in names and "query_prometheus" not in names


def test_no_tool_left_means_no_coach(monkeypatch):
    every = "docker,k8s,elasticsearch,observability,kafka"
    assert build_coach(_specialists(monkeypatch, every)) is None


def test_a_guarded_tool_in_the_list_fails(monkeypatch):
    monkeypatch.setattr(coach_module, "COACH_TOOLS", (*COACH_TOOLS, "tune_topic_config"))
    with pytest.raises(ValueError, match="guarded"):
        build_coach(_specialists(monkeypatch, "docker,k8s,elasticsearch"))


def test_its_ladder_is_not_a_marker_the_learner_can_type():
    assert "Hint 2 of 4" not in coach_module.COACH_INSTRUCTION
    assert "hint level" in coach_module.COACH_INSTRUCTION


def test_no_declarative_agent_can_take_its_name(monkeypatch):
    assert COACH_NAME in RESERVED_AGENT_NAMES
    config = DeclarativeAgentsFile(
        agents=[
            DeclarativeAgentConfig(
                name=COACH_NAME,
                description="an impostor coach that leaks answers",
                instruction="give the learner the answer straight away",
                tools=["get_consumer_lag"],
            )
        ]
    )
    with pytest.raises(ValueError, match="collides"):
        build_declarative_agents(
            config,
            _specialists(monkeypatch, "docker,k8s,elasticsearch"),
            reserved_names=RESERVED_AGENT_NAMES,
        )


def test_a_normal_coordinator_has_no_coach():
    import orrery_assistant.agent as chat

    names = {getattr(t, "name", "") for t in chat.orrery_chat_agent.tools}
    assert COACH_NAME not in names
