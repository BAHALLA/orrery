"""Tests for TokenBudgetPlugin: daily model-token ceilings per caller and per deployment."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from orrery_core.plugins import TokenBudgetPlugin, default_plugins
from orrery_core.plugins.token_budget_plugin import (
    GLOBAL_KEY,
    InMemoryUsageLedger,
    StorageUsageLedger,
    create_usage_ledger,
)


class _MeteredLlm(BaseLlm):
    """Answers every call with a fixed token bill; counts the calls it served."""

    model: str = "metered"
    tokens: int = 100
    calls: int = 0

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.calls += 1
        if stream:
            # A partial chunk repeats the final's counts; it must not be charged.
            yield LlmResponse(
                partial=True,
                content=types.Content(role="model", parts=[types.Part.from_text(text="ok")]),
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    total_token_count=self.tokens
                ),
            )
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part.from_text(text="ok")]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=self.tokens - 10,
                candidates_token_count=10,
                total_token_count=self.tokens,
            ),
        )


async def _turn(runner: InMemoryRunner, user_id: str) -> str:
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id=user_id)
    text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text="hi")]),
    ):
        if event.content and event.content.parts:
            text += "".join(p.text or "" for p in event.content.parts)
    return text


def _runner(plugin: TokenBudgetPlugin) -> tuple[InMemoryRunner, _MeteredLlm]:
    llm = _MeteredLlm()
    agent = LlmAgent(name="root", model=llm, instruction="Answer.")
    return InMemoryRunner(agent=agent, app_name="t", plugins=[plugin]), llm


@pytest.mark.asyncio
async def test_a_caller_is_refused_once_their_day_is_spent_and_others_are_not():
    ledger = InMemoryUsageLedger()
    runner, llm = _runner(TokenBudgetPlugin(per_user=150, ledger=ledger, today=lambda: "d1"))

    assert await _turn(runner, "alice") == "ok"  # 100 spent
    assert await _turn(runner, "alice") == "ok"  # 200 spent: the crossing call finishes
    refused = await _turn(runner, "alice")
    assert "usage limit" in refused
    assert llm.calls == 2  # the refusal bought no tokens
    assert ledger.spent("alice", "d1") == 200

    assert await _turn(runner, "bob") == "ok"


@pytest.mark.asyncio
async def test_the_deployment_ceiling_refuses_everyone():
    runner, llm = _runner(TokenBudgetPlugin(total=100, today=lambda: "d1"))

    assert await _turn(runner, "alice") == "ok"
    assert "usage limit for today" in await _turn(runner, "bob")
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_a_new_day_starts_with_nothing_spent():
    day = {"now": "d1"}
    runner, _ = _runner(TokenBudgetPlugin(per_user=50, today=lambda: day["now"]))

    assert await _turn(runner, "alice") == "ok"
    assert "usage limit" in await _turn(runner, "alice")
    day["now"] = "d2"
    assert await _turn(runner, "alice") == "ok"


@pytest.mark.asyncio
async def test_streamed_partials_are_not_charged_twice():
    from google.adk.agents.run_config import RunConfig, StreamingMode

    ledger = InMemoryUsageLedger()
    runner, _ = _runner(TokenBudgetPlugin(total=10_000, ledger=ledger, today=lambda: "d1"))
    session = await runner.session_service.create_session(app_name="t", user_id="alice")
    async for _ in runner.run_async(
        user_id="alice",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text="hi")]),
        run_config=RunConfig(streaming_mode=StreamingMode.SSE),
    ):
        pass
    assert ledger.spent(GLOBAL_KEY, "d1") == 100


def test_the_in_memory_ledger_keeps_only_today():
    ledger = InMemoryUsageLedger()
    ledger.charge("alice", "d1", 5)
    ledger.charge("alice", "d2", 7)
    assert ledger.spent("alice", "d1") == 0
    assert ledger.spent("alice", "d2") == 7


def test_a_shared_ledger_counts_in_limits_storage():
    pytest.importorskip("limits")
    ledger = StorageUsageLedger("memory://")
    ledger.charge("alice", "d1", 5)
    ledger.charge("alice", "d1", 7)
    assert ledger.spent("alice", "d1") == 12
    assert ledger.spent("alice", "d2") == 0


def test_the_ledger_factory_picks_by_uri():
    assert isinstance(create_usage_ledger(None), InMemoryUsageLedger)
    assert isinstance(create_usage_ledger("memory://"), InMemoryUsageLedger)


def test_negative_budgets_are_refused():
    with pytest.raises(ValueError, match=">= 0"):
        TokenBudgetPlugin(per_user=-1)


def _budget_plugins(**kwargs) -> list[TokenBudgetPlugin]:
    return [p for p in default_plugins(**kwargs) if isinstance(p, TokenBudgetPlugin)]


def test_default_plugins_leaves_it_out_unless_a_budget_is_set(monkeypatch):
    monkeypatch.delenv("ORRERY_DAILY_TOKEN_BUDGET_PER_USER", raising=False)
    monkeypatch.delenv("ORRERY_DAILY_TOKEN_BUDGET", raising=False)
    assert _budget_plugins() == []

    monkeypatch.setenv("ORRERY_DAILY_TOKEN_BUDGET_PER_USER", "200000")
    (plugin,) = _budget_plugins()
    assert (plugin.per_user, plugin.total) == (200_000, 0)

    (plugin,) = _budget_plugins(daily_token_budget=5, daily_token_budget_per_user=0)
    assert (plugin.per_user, plugin.total) == (0, 5)


def test_a_malformed_budget_fails_startup(monkeypatch):
    monkeypatch.setenv("ORRERY_DAILY_TOKEN_BUDGET", "lots")
    with pytest.raises(ValueError, match="ORRERY_DAILY_TOKEN_BUDGET"):
        default_plugins()


def test_it_runs_ahead_of_the_other_model_callbacks(monkeypatch):
    monkeypatch.setenv("ORRERY_DAILY_TOKEN_BUDGET", "5")
    names = [type(p).__name__ for p in default_plugins(enable_auth=True)]
    budget = names.index("TokenBudgetPlugin")
    assert names.index("AuthPlugin") < budget < names.index("CallBudgetPlugin")
