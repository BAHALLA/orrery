"""The operators' platform context and triage scope reach the prompts that need them."""

from __future__ import annotations

import pytest

from orrery_assistant import agent as chat
from orrery_core.agent import platform
from orrery_core.agent.base import base_instruction


@pytest.fixture
def context_file(tmp_path, monkeypatch):
    path = tmp_path / "platform.md"
    path.write_text("Critical: nginx, api, keycloak. Auxiliary: lab-*.")
    monkeypatch.setenv(platform.PLATFORM_CONTEXT_ENV, str(path))
    platform.load_platform_context.cache_clear()
    yield
    platform.load_platform_context.cache_clear()


def test_the_coordinator_and_the_sweep_carry_the_context(context_file):
    specialists = chat._specialists
    coordinator = base_instruction(chat.build_chat_agent(specialists, []))
    triage = base_instruction(chat.build_triage_agent(specialists))
    checker = base_instruction(chat.build_health_checker(chat.sweepable(specialists)[0]))
    for text in (coordinator, triage, checker):
        assert "## Platform context (from the operators)" in text
        assert "Critical: nginx, api, keycloak." in text


def test_no_context_configured_adds_nothing(monkeypatch):
    monkeypatch.delenv(platform.PLATFORM_CONTEXT_ENV, raising=False)
    platform.load_platform_context.cache_clear()
    coordinator = base_instruction(chat.build_chat_agent(chat._specialists, []))
    assert "Platform context" not in coordinator


def test_triage_never_lets_auxiliary_resources_decide(monkeypatch):
    monkeypatch.setenv(platform.AUXILIARY_ENV, "lab-*")
    triage = base_instruction(chat.build_triage_agent(chat._specialists))
    assert "`scope: auxiliary`" in triage
    assert "never let them raise a status or the overall verdict" in triage


def test_examples_name_only_systems_the_coordinator_runs(monkeypatch):
    """With the kafka specialist off, the coordinator's examples must not be about Kafka."""
    from orrery_core.registry import discover_specialists

    monkeypatch.delenv("ORRERY_SPECIALISTS", raising=False)
    monkeypatch.delenv("ORRERY_DISABLED_SPECIALISTS", raising=False)
    everything = discover_specialists(record=False)
    assert any(s.key == "kafka" for s in everything)
    without_kafka = [s for s in everything if s.key != "kafka"]
    assert "kafka" not in base_instruction(chat.build_chat_agent(without_kafka, [])).lower()
