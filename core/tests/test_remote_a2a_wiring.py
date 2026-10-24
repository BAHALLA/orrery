"""ADR-008: ``RemoteA2aAgent`` is used in exactly one place.

Everything Orrery sends to a remote A2A agent goes through
:class:`~orrery_core.a2a.remote.GuardedRemoteA2aTool`, which pins the card,
guards the network, sets the hop header and never forwards the caller's token.
A second use of ``RemoteA2aAgent`` (say, as a sub-agent) would bypass all of
that, so this test fails if one appears anywhere in the shipped code.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = ROOT / "core" / "orrery_core" / "a2a" / "remote.py"
SOURCES = ("core/orrery_core", "agents", "deploy")
NAME = "RemoteA2aAgent"
MODULE = "google.adk.agents.remote_a2a_agent"


def _uses(source: str) -> list[int]:
    """Lines that import, name or reach ``RemoteA2aAgent`` (comments do not count)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.module == MODULE or any(a.name == NAME for a in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == MODULE for a in node.names):
                lines.append(node.lineno)
        elif (isinstance(node, ast.Name) and node.id == NAME) or (
            isinstance(node, ast.Attribute) and node.attr == NAME
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and node.value in (NAME, MODULE):
            lines.append(node.lineno)  # importlib / getattr by name
    return lines


def test_remote_a2a_agent_is_only_used_by_the_guarded_tool():
    offenders = []
    for top in SOURCES:
        for path in (ROOT / top).rglob("*.py"):
            if path == ALLOWED or "/tests/" in path.as_posix() or ".venv" in path.parts:
                continue
            offenders += [
                f"{path.relative_to(ROOT)}:{n}" for n in _uses(path.read_text(encoding="utf-8"))
            ]
    assert not offenders, "RemoteA2aAgent outside a2a/remote.py:\n" + "\n".join(offenders)


def test_the_guarded_tool_does_use_it():
    assert _uses(ALLOWED.read_text(encoding="utf-8"))
