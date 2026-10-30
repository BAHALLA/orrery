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


def _without(key: str):
    return [s for s in chat._specialists if s.key != key]


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


def test_triage_never_lets_auxiliary_resources_decide():
    triage = base_instruction(chat.build_triage_agent(chat._specialists))
    assert "`scope: auxiliary`" in triage
    assert "never let them raise a status or the overall verdict" in triage
    assert "`scope: auxiliary`" in base_instruction(chat.triage_summarizer)


def test_examples_name_only_systems_the_coordinator_runs():
    """With the kafka specialist off, the coordinator's examples must not be about Kafka."""
    if not any(s.key == "kafka" for s in chat._specialists):
        pytest.skip("kafka specialist not enabled here")
    without_kafka = base_instruction(chat.build_chat_agent(_without("kafka"), []))
    assert "kafka" not in without_kafka.lower()
