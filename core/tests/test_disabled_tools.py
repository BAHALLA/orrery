"""ORRERY_DISABLED_TOOLS: one tool at a time out of a specialist, never in."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.tools.function_tool import FunctionTool

from orrery_core import confirm, create_agent, is_confirmation_gate, registry
from orrery_core.agent.declarative import tool_catalogue
from orrery_core.plugins.guardrails_plugin import GuardrailsPlugin
from orrery_core.registry import (
    HealthCheck,
    SpecialistSpec,
    discover_specialists,
    is_tool_disabled,
)
from orrery_core.reliability.resilience import ToolOutcome, classify_tool_outcome


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for env in ("ORRERY_SPECIALISTS", "ORRERY_DISABLED_SPECIALISTS", "ORRERY_DISABLED_TOOLS"):
        monkeypatch.delenv(env, raising=False)
    # discover_specialists() replaces the record; restore it after each test.
    monkeypatch.setattr(registry, "_DISABLED_FUNCS", {})


async def get_lag() -> dict:
    """Read lag."""
    return {}


async def get_health() -> dict:
    """Read health."""
    return {}


@confirm("resets offsets")
async def reset_offsets() -> dict:
    """Reset."""
    return {}


@dataclass
class _Dist:
    name: str


@dataclass
class _EP:
    name: str
    obj: Any
    dist: _Dist
    value: str = "pkg.agent:root_agent"

    def load(self) -> Any:
        return self.obj


def _kafka(**spec: Any) -> SpecialistSpec:
    agent = create_agent(
        name="kafka_agent",
        model="gemini-x",
        description="Kafka.",
        instruction="",
        tools=[get_lag, get_health, reset_offsets],
    )
    return SpecialistSpec(agent=agent, **spec)


def _loader(*pairs: tuple[str, Any]):
    return lambda: [_EP(k, v, _Dist("kafka-health-agent")) for k, v in pairs]


def _names(agent: Any) -> list[str]:
    return [registry._tool_name(t) for t in agent.tools]


def test_unset_changes_nothing():
    spec = _kafka()
    [s] = discover_specialists(loader=_loader(("kafka", spec)))
    assert s.agent is spec.agent
    assert not is_tool_disabled(get_lag)


def test_removes_the_tool_from_a_copy_and_leaves_the_original(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_lag")
    spec = _kafka()

    [s] = discover_specialists(loader=_loader(("kafka", spec)))

    assert _names(s.agent) == ["get_health", "reset_offsets"]
    assert _names(spec.agent) == ["get_lag", "get_health", "reset_offsets"]
    assert is_tool_disabled(get_lag) and is_tool_disabled(FunctionTool(get_lag))
    assert not is_tool_disabled(get_health)
    # The clone keeps the confirmation gate for the guarded tool it still holds.
    assert is_confirmation_gate(getattr(s.agent, "before_tool_callback", None))


def test_the_declarative_catalogue_never_sees_a_removed_tool(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_lag")
    specialists = discover_specialists(loader=_loader(("kafka", _kafka())))

    catalogue, _ = tool_catalogue(specialists)
    assert "get_lag" not in catalogue and "kafka.get_lag" not in catalogue
    assert "kafka.get_health" in catalogue


def test_a_later_discovery_replaces_the_record(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_lag")
    discover_specialists(loader=_loader(("kafka", _kafka())))
    monkeypatch.delenv("ORRERY_DISABLED_TOOLS")
    discover_specialists(loader=_loader(("kafka", _kafka())))
    assert not is_tool_disabled(get_lag)


def test_a_discovery_that_does_not_record_leaves_the_record(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_lag")
    discover_specialists(loader=_loader(("kafka", _kafka())))
    monkeypatch.delenv("ORRERY_DISABLED_TOOLS")
    discover_specialists(loader=_loader(("kafka", _kafka())), record=False)
    assert is_tool_disabled(get_lag)


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("get_lag", "must be a qualified name"),
        ("kafka.", "must be a qualified name"),
        ("kafka.a.b", "must be a qualified name"),
        ("kafak.get_lag", "not installed"),
        ("kafka.get_lagg", "has no tool \\['get_lagg'\\]"),
    ],
)
def test_a_bad_name_stops_startup(monkeypatch, value, match):
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", value)
    with pytest.raises(ValueError, match=match):
        discover_specialists(loader=_loader(("kafka", _kafka())))


def test_a_tool_of_a_specialist_that_is_not_running_stops_startup(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", "kafka")
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_lag")
    with pytest.raises(ValueError, match="not running: \\['kafka'\\]"):
        discover_specialists(loader=_loader(("kafka", _kafka())))


def test_a_tool_the_health_check_uses_cannot_be_removed(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_health")
    check = HealthCheck(system="Kafka", checks="health.", criteria="down.", tools=(get_health,))
    with pytest.raises(ValueError, match="used by the specialist's health_check"):
        discover_specialists(loader=_loader(("kafka", _kafka(health_check=check))))


def _other(**spec: Any) -> SpecialistSpec:
    """A second specialist that re-exports the first one's get_lag function."""
    agent = create_agent(
        name="lag_agent", model="gemini-x", description="Lag.", instruction="", tools=[get_lag]
    )
    return SpecialistSpec(agent=agent, **spec)


def test_a_shared_function_is_removed_from_every_specialist(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_lag")
    other = _other()
    found = {
        s.key: s for s in discover_specialists(loader=_loader(("kafka", _kafka()), ("lag", other)))
    }
    assert "get_lag" not in _names(found["lag"].agent)
    assert _names(other.agent) == ["get_lag"]


def test_a_shared_function_another_sweep_uses_stops_startup(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_lag")
    check = HealthCheck(system="Lag", checks="lag.", criteria="high.", tools=(get_lag,))
    loader = _loader(("kafka", _kafka()), ("lag", _other(health_check=check)))
    with pytest.raises(ValueError, match="also used by the lag specialist's health_check"):
        discover_specialists(loader=loader)


@pytest.mark.asyncio
async def test_guardrails_refuse_a_removed_tool_with_a_neutral_status(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_lag")
    discover_specialists(loader=_loader(("kafka", _kafka())))

    plugin = GuardrailsPlugin()
    refused = await plugin.before_tool_callback(
        tool=FunctionTool(get_lag), tool_args={}, tool_context=MagicMock()
    )

    assert refused is not None and refused["status"] == "BLOCKED"
    assert "ORRERY_DISABLED_TOOLS" in refused["message"]
    # A refusal says nothing about the tool's health: it must never open a circuit.
    assert classify_tool_outcome(refused) is ToolOutcome.IGNORE


def test_narrowing_works_on_the_real_workspace(monkeypatch):
    monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", "docker,k8s,elasticsearch")
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.describe_consumer_groups")

    specialists = {s.key: s for s in discover_specialists(record=False)}

    assert "describe_consumer_groups" not in _names(specialists["kafka"].agent)
    assert "get_consumer_lag" in _names(specialists["kafka"].agent)


def test_a_tool_the_real_kafka_sweep_uses_cannot_be_removed(monkeypatch):
    # get_consumer_lag feeds the Kafka health check, so the triage sweep would
    # still call it; removing it from the chat agent alone would be a false promise.
    monkeypatch.setenv("ORRERY_DISABLED_TOOLS", "kafka.get_consumer_lag")
    with pytest.raises(ValueError, match="used by the specialist's health_check"):
        discover_specialists(record=False)
