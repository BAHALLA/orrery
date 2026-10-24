"""The Studio's per-run plugin stack (AEP-031 §2) and the shared daily token ledger."""

from __future__ import annotations

from orrery_core.plugins import default_plugins
from orrery_core.plugins.autonomy_plugin import AutonomyPlugin
from orrery_core.plugins.delegation_guard_plugin import DelegationGuardPlugin
from orrery_core.plugins.memory_plugin import MemoryPlugin
from orrery_core.plugins.token_budget_plugin import TokenBudgetPlugin, shared_usage_ledger
from orrery_core.security.principal import is_machine_session
from orrery_core.studio.runner import StudioRunPlugin, _insert_before_error_handler, studio_plugins


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


def test_the_run_plugin_goes_before_the_error_handler():
    plugins = studio_plugins()
    marker = StudioRunPlugin.__new__(StudioRunPlugin)
    _insert_before_error_handler(plugins, marker)
    assert plugins[-2] is marker and type(plugins[-1]).__name__ == "ErrorHandlerPlugin"


def test_studio_sessions_are_machine_sessions():
    assert is_machine_session("s-0123abcd")
    assert not is_machine_session("0123abcd")
