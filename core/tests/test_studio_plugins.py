"""The Studio's per-run plugin stack (AEP-031 §2) and the shared daily token ledger."""

from __future__ import annotations

import contextlib
from typing import Any, cast

from orrery_core.plugins import default_plugins
from orrery_core.plugins.autonomy_plugin import AutonomyPlugin
from orrery_core.plugins.delegation_guard_plugin import DelegationGuardPlugin
from orrery_core.plugins.memory_plugin import MemoryPlugin
from orrery_core.plugins.token_budget_plugin import TokenBudgetPlugin, shared_usage_ledger
from orrery_core.security.principal import is_machine_session
from orrery_core.studio.runner import (
    StudioRunPlugin,
    StudioSpentGate,
    _place_run_plugins,
    studio_plugins,
)


def _of(plugins, kind):
    return [p for p in plugins if isinstance(p, kind)]


def test_each_run_gets_fresh_plugins_but_one_daily_token_ledger(monkeypatch):
    monkeypatch.setenv("ORRERY_DAILY_TOKEN_BUDGET_PER_USER", "1000")
    chat = default_plugins()
    run_a, run_b = studio_plugins(), studio_plugins()

    [chat_budget] = _of(chat, TokenBudgetPlugin)
    [a_budget] = _of(run_a, TokenBudgetPlugin)
    [b_budget] = _of(run_b, TokenBudgetPlugin)
    assert a_budget is not b_budget
    # A fresh ledger per run would start every run at zero spent.
    assert a_budget._ledger is b_budget._ledger is chat_budget._ledger
    assert a_budget._ledger is shared_usage_ledger(None)

    a_budget._ledger.charge("user:alice", "2026-10-24", 600)
    assert chat_budget._ledger.spent("user:alice", "2026-10-24") == 600

    # Stateful guards are never shared with chat or between runs.
    assert _of(run_a, DelegationGuardPlugin)[0] is not _of(run_b, DelegationGuardPlugin)[0]


def test_runs_are_held_to_l2_and_skip_memory(monkeypatch):
    monkeypatch.setenv("ORRERY_AUTONOMY_LEVEL", "L4")
    plugins = studio_plugins()
    [autonomy] = _of(plugins, AutonomyPlugin)
    assert autonomy._level == "L2"  # the configured L4 does not reach a run
    assert _of(plugins, MemoryPlugin) == []


def test_the_run_plugins_sit_before_the_output_cap_and_the_token_budget(monkeypatch):
    monkeypatch.setenv("ORRERY_DAILY_TOKEN_BUDGET_PER_USER", "1000")
    plugins = studio_plugins()
    run_plugin = StudioRunPlugin.__new__(StudioRunPlugin)
    gate = StudioSpentGate.__new__(StudioSpentGate)
    _place_run_plugins(plugins, run_plugin, gate)
    names = [type(p).__name__ for p in plugins]
    assert names.index("StudioRunPlugin") == names.index("ToolOutputCapPlugin") - 1
    assert names.index("StudioSpentGate") == names.index("TokenBudgetPlugin") - 1
    assert names[-1] == "ErrorHandlerPlugin"


def test_studio_sessions_are_machine_sessions():
    assert is_machine_session("s-0123abcd")
    assert not is_machine_session("0123abcd")


# ── Compiler wiring: MCP pins and TLS ────────────────────────────────


def _mcp_registry():
    from orrery_core.studio.registry import StudioRegistry

    return StudioRegistry.model_validate(
        {
            "mcp": [
                {
                    "id": "docs",
                    "url": "https://docs.example.com/mcp",
                    "transport": "streamable_http",
                    "tools": ["search"],
                    "read_only": ["search"],
                }
            ]
        }
    )


def _one_node_spec():
    from orrery_core.studio.spec import parse_spec

    return parse_spec(
        {
            "spec_version": 1,
            "name": "s",
            "nodes": [
                {"id": "a", "kind": "llm_agent", "instruction": "x", "tools": ["mcp:docs.search"]}
            ],
        }
    )


def test_mcp_pins_outlive_a_run(tmp_path):
    """A description that changes between two runs is noticed (one store per process)."""
    from types import SimpleNamespace

    from orrery_core.a2a.remote import RemoteCards
    from orrery_core.studio.compiler import McpPins, compile_spec

    pins = McpPins()
    compiled = [
        compile_spec(
            _one_node_spec(),
            registry=_mcp_registry(),
            cards=RemoteCards(),
            catalogue={},
            model="gemini-2.5-flash",
            mcp_pins=pins,
        )
        for _ in range(2)
    ]
    first, second = (cast(Any, c.toolsets[0]) for c in compiled)
    served = SimpleNamespace(name="search", description="Search the docs.", raw_mcp_tool=None)
    assert first._refusal(served) is None  # first seen: pinned
    changed = SimpleNamespace(
        name="search", description="Search the docs and the wiki.", raw_mcp_tool=None
    )
    assert second._refusal(changed) is not None  # the next run's toolset still remembers


def test_studio_mcp_entries_use_the_deployment_ca(tmp_path, monkeypatch):
    from orrery_core.a2a.remote import RemoteCards, RemoteNetwork
    from orrery_core.studio import compiler

    seen: list[dict] = []

    class Recorder:
        def __init__(self, config, **kwargs):
            seen.append(kwargs)

        async def close(self):
            return None

    monkeypatch.setattr(compiler, "GuardedMcpToolset", Recorder)
    network = RemoteNetwork()
    sentinel = object()
    monkeypatch.setattr(RemoteNetwork, "ssl_context", sentinel)
    # An agent may refuse the recorder; the toolset was built first.
    with contextlib.suppress(Exception):
        compiler.compile_spec(
            _one_node_spec(),
            registry=_mcp_registry(),
            cards=RemoteCards(network),
            catalogue={},
            model="gemini-2.5-flash",
        )
    assert seen and seen[0]["verify"] is sentinel


# ── A run cancelled before it starts ─────────────────────────────────


def test_a_run_cancelled_before_its_first_step_ends_cancelled():
    import asyncio

    from orrery_core.a2a.remote import RemoteCards
    from orrery_core.security.auth import AuthContext
    from orrery_core.studio.config import StudioConfig
    from orrery_core.studio.registry import StudioRegistry
    from orrery_core.studio.runner import StudioService
    from orrery_core.studio.spec import parse_spec

    service = StudioService(
        config=StudioConfig(enabled=True), registry=StudioRegistry(), cards=RemoteCards()
    )
    spec = parse_spec(
        {
            "spec_version": 1,
            "name": "s",
            "nodes": [{"id": "a", "kind": "llm_agent", "instruction": "x"}],
        }
    )

    async def go():
        run = service.start(spec, "go", AuthContext(subject="alice", role="admin"))
        service.cancel(run)  # before the task's first step
        assert run.task is not None
        with contextlib.suppress(asyncio.CancelledError):
            await run.task
        await asyncio.sleep(0)  # done callbacks run on the next loop turn
        return run

    run = asyncio.run(go())
    assert run.status == "cancelled" and run.finished
    assert run.events[-1]["type"] == "run.end" and run.events[-1]["outcome"] == "cancelled"
    assert service.running("alice") == 0
