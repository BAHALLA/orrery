"""The shipped lab coach must build where it runs, and stay read-only.

``declarative/lab-coach.yaml`` is loaded by a shared lab that disables the
docker, k8s and elasticsearch specialists and pins autonomy L2. A declarative
agent naming a tool from a disabled specialist stops startup, so the file is
built here against exactly that specialist set. A guarded tool slipped into the
list would not fail anything at runtime (L2 refuses it), but it would put a
confirmation gate on a coach whose whole design is that learners cannot
change the lab, so that fails too.
"""

from __future__ import annotations

from pathlib import Path

from orrery_assistant.agent import RESERVED_AGENT_NAMES
from orrery_core.agent.declarative import build_declarative_agents, load_agents_config
from orrery_core.registry import discover_specialists
from orrery_core.security.guardrails import get_guard_level, is_confirmation_gate

COACH_FILE = Path(__file__).parents[1] / "declarative" / "lab-coach.yaml"


def _build(monkeypatch, disabled: str):
    monkeypatch.delenv("ORRERY_SPECIALISTS", raising=False)
    monkeypatch.setenv("ORRERY_DISABLED_SPECIALISTS", disabled)
    specialists = discover_specialists(record=False)
    return build_declarative_agents(
        load_agents_config(COACH_FILE), specialists, reserved_names=RESERVED_AGENT_NAMES
    )


def test_builds_with_the_lab_specialist_set(monkeypatch):
    (coach,) = _build(monkeypatch, "docker,k8s,elasticsearch")
    assert coach.name == "lab_coach"


def test_builds_with_every_specialist(monkeypatch):
    (coach,) = _build(monkeypatch, "")
    assert coach.name == "lab_coach"


def test_is_read_only(monkeypatch):
    (coach,) = _build(monkeypatch, "docker,k8s,elasticsearch")
    guarded = {t.name: lvl for t in coach.tools if (lvl := get_guard_level(t))}
    assert guarded == {}
    assert not is_confirmation_gate(coach.before_tool_callback)
