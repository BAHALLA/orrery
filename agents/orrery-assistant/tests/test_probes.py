"""The console's first-run self-test is built from the specialists that run."""

from __future__ import annotations

import pytest
from google.adk.agents import LlmAgent

from orrery_assistant import agent as chat
from orrery_assistant.probes import default_probes
from orrery_core.registry import SetupCheck, Specialist


async def ping() -> dict:
    """Read a status."""
    return {"status": "ok", "pong": True}


def _redis(**extra) -> Specialist:
    agent = LlmAgent(name="redis_agent", model="gemini-x", tools=[ping])
    return Specialist("redis", agent, "acme-redis-agent", **extra)


def test_every_built_in_integration_declares_its_setup_check():
    # The rows the console showed before the probes moved into the specialists.
    probes = {p.name: p.label for p in default_probes(chat._specialists)}

    assert probes == {
        "docker": "Docker",
        "elasticsearch": "Elasticsearch",
        "k8s": "Kubernetes",
        "kafka": "Kafka",
        "observability": "Prometheus",
    }


def test_a_switched_off_specialist_is_not_probed():
    running = [s for s in chat._specialists if s.key not in {"docker", "k8s"}]

    assert {p.name for p in default_probes(running)} == {"elasticsearch", "kafka", "observability"}


def test_a_specialist_without_a_setup_check_has_no_row():
    assert default_probes([_redis()]) == []


@pytest.mark.asyncio
async def test_a_third_party_specialist_is_probed_through_its_own_tool():
    check = SetupCheck(label="Redis", tool=ping, hint="Set REDIS_URL.")

    [probe] = default_probes([_redis(setup_check=check)])

    assert (probe.name, probe.label, probe.hint) == ("redis", "Redis", "Set REDIS_URL.")
    assert await probe.run() == {"status": "ok", "pong": True}
