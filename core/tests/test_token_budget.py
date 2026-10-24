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
    user_key,
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


async def _turn(runner: InMemoryRunner, user_id: str, message: str = "hi") -> str:
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id=user_id)
    text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=message)]),
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
    assert ledger.spent(user_key("alice"), "d1") == 200

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


class _GatedLlm(_MeteredLlm):
    """Holds every call until *release* is set, like a slow provider."""

    model: str = "gated"
    release: Any = None

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        await self.release.wait()
        async for response in super().generate_content_async(llm_request, stream):
            yield response


@pytest.mark.asyncio
async def test_calls_started_together_see_each_others_prompts():
    import asyncio

    llm = _GatedLlm(release=asyncio.Event())
    plugin = TokenBudgetPlugin(per_user=1000, today=lambda: "d1")
    runner = InMemoryRunner(
        agent=LlmAgent(name="root", model=llm, instruction="Answer."),
        app_name="t",
        plugins=[plugin],
    )
    big = "x" * 4000  # about 1000 tokens, reserved before the call goes out
    first = asyncio.create_task(_turn(runner, "alice", big))
    while not plugin._reserved:
        await asyncio.sleep(0)
    # Without the reservation nothing is spent yet, and this would go out too.
    assert "usage limit" in await _turn(runner, "alice", big)
    llm.release.set()
    assert await first == "ok"
    assert llm.calls == 1


class _BrokenLedger(InMemoryUsageLedger):
    def spent(self, key: str, day: str) -> int:
        raise ConnectionError("redis down")

    def charge(self, key: str, day: str, tokens: int) -> None:
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_a_failing_store_lets_the_call_through_and_keeps_the_answer():
    from orrery_core.observability.metrics import TOKEN_BUDGET_STORE_ERRORS_TOTAL

    before = TOKEN_BUDGET_STORE_ERRORS_TOTAL._value.get()
    runner, llm = _runner(TokenBudgetPlugin(per_user=10, ledger=_BrokenLedger()))
    assert await _turn(runner, "alice") == "ok"
    assert llm.calls == 1
    assert TOKEN_BUDGET_STORE_ERRORS_TOTAL._value.get() > before


@pytest.mark.asyncio
async def test_a_slow_store_does_not_hold_the_call(monkeypatch):
    import threading

    import orrery_core.plugins.token_budget_plugin as mod

    monkeypatch.setattr(mod, "_STORE_TIMEOUT_SECONDS", 0.05)
    stuck = threading.Event()

    class _HungLedger(InMemoryUsageLedger):
        blocking = True

        def spent(self, key: str, day: str) -> int:
            stuck.wait(5)
            return 0

    runner, _ = _runner(TokenBudgetPlugin(per_user=10, ledger=_HungLedger()))
    try:
        assert await _turn(runner, "alice") == "ok"
    finally:
        stuck.set()


class _SilentLlm(_MeteredLlm):
    """Reports no usage at all."""

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part.from_text(text="ok")])
        )


@pytest.mark.asyncio
async def test_without_usage_the_estimate_is_charged():
    ledger = InMemoryUsageLedger()
    plugin = TokenBudgetPlugin(per_user=10_000, ledger=ledger, today=lambda: "d1")
    runner = InMemoryRunner(
        agent=LlmAgent(name="root", model=_SilentLlm(), instruction="Answer."),
        app_name="t",
        plugins=[plugin],
    )
    assert await _turn(runner, "alice", "x" * 4000) == "ok"
    assert ledger.spent(user_key("alice"), "d1") >= 1000


class _FailingLlm(_MeteredLlm):
    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        raise RuntimeError("provider down")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_a_failed_call_refunds_its_estimate():
    ledger = InMemoryUsageLedger()
    plugin = TokenBudgetPlugin(per_user=10_000, ledger=ledger, today=lambda: "d1")
    runner = InMemoryRunner(
        agent=LlmAgent(name="root", model=_FailingLlm(), instruction="Answer."),
        app_name="t",
        plugins=[plugin],
    )
    with pytest.raises(RuntimeError, match="provider down"):
        await _turn(runner, "alice", "x" * 4000)
    assert ledger.spent(user_key("alice"), "d1") == 0
    assert plugin._reserved == {}


class _DelegatingLlm(BaseLlm):
    """Asks the specialist once, then answers with whatever came back."""

    model: str = "delegating"

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        answered = [
            part.function_response
            for content in llm_request.contents
            for part in content.parts or []
            if part.function_response
        ]
        part = (
            types.Part.from_text(text=f"done: {answered[-1].response}")
            if answered
            else types.Part.from_function_call(name="specialist", args={"request": "check"})
        )
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=10),
        )


def _delegating_runner(plugin: TokenBudgetPlugin) -> tuple[InMemoryRunner, _MeteredLlm]:
    from google.adk.tools.agent_tool import AgentTool

    specialist_llm = _MeteredLlm()
    specialist = LlmAgent(name="specialist", model=specialist_llm, instruction="Check.")
    root = LlmAgent(
        name="root", model=_DelegatingLlm(), instruction="Route.", tools=[AgentTool(specialist)]
    )
    return InMemoryRunner(agent=root, app_name="t", plugins=[plugin]), specialist_llm


@pytest.mark.asyncio
async def test_a_specialists_calls_are_charged_to_the_caller():
    ledger = InMemoryUsageLedger()
    runner, specialist_llm = _delegating_runner(
        TokenBudgetPlugin(per_user=10_000, ledger=ledger, today=lambda: "d1")
    )
    assert (await _turn(runner, "alice")).startswith("done")
    assert specialist_llm.calls == 1
    assert ledger.spent(user_key("alice"), "d1") == 10 + 100 + 10


@pytest.mark.asyncio
async def test_a_refusal_inside_a_specialist_ends_the_turn_cleanly():
    # The root's first call (10 tokens) crosses the line; everything after is refused.
    runner, specialist_llm = _delegating_runner(TokenBudgetPlugin(per_user=5, today=lambda: "d1"))
    assert "usage limit" in await _turn(runner, "alice")
    assert specialist_llm.calls == 0


def test_ledger_keys_are_printable_and_do_not_carry_the_subject():
    key = user_key("alice@example.com")
    assert key.isprintable() and "alice" not in key and len(key) < 64
    assert GLOBAL_KEY.isprintable()


def test_per_replica_budgets_are_warned_about(monkeypatch, caplog):
    monkeypatch.setenv("ORRERY_MULTI_REPLICA", "true")
    monkeypatch.delenv("ORRERY_RATE_LIMIT_STORAGE_URI", raising=False)
    with caplog.at_level("WARNING", logger="orrery.plugins"):
        _budget_plugins(daily_token_budget=5)
    assert "per replica" in caplog.text


def test_a_settle_after_midnight_leaves_today_alone():
    ledger = InMemoryUsageLedger()
    ledger.charge("u", "2026-10-24", 500)
    ledger.charge("u", "2026-10-25", 900)
    ledger.charge("u", "2026-10-24", -400)  # yesterday's call settling late
    assert ledger.spent("u", "2026-10-25") == 900
    ledger.charge("v", "2026-10-25", 1)
    assert ledger.spent("u", "2026-10-25") == 900


class _ChargeFailsOnce(InMemoryUsageLedger):
    """The reservation's write fails; everything after works."""

    failed = False

    def charge(self, key: str, day: str, tokens: int) -> None:
        if not self.failed:
            self.failed = True
            raise ConnectionError("blip")
        super().charge(key, day, tokens)


@pytest.mark.asyncio
async def test_a_reservation_that_failed_is_not_deducted_at_settle():
    ledger = _ChargeFailsOnce()
    runner = InMemoryRunner(
        agent=LlmAgent(name="root", model=_MeteredLlm(), instruction="Answer."),
        app_name="t",
        plugins=[TokenBudgetPlugin(per_user=10_000, ledger=ledger, today=lambda: "d1")],
    )
    assert await _turn(runner, "alice", "x" * 4000) == "ok"
    assert ledger.spent(user_key("alice"), "d1") == 100


def test_the_estimate_counts_tool_declarations():
    from google.adk.models.llm_request import LlmRequest

    from orrery_core.plugins.token_budget_plugin import estimate_prompt_tokens

    declaration = types.FunctionDeclaration(name="get_pods", description="d" * 4000)
    request = LlmRequest(
        config=types.GenerateContentConfig(tools=[types.Tool(function_declarations=[declaration])])
    )
    assert estimate_prompt_tokens(request) >= 1000


@pytest.mark.asyncio
async def test_a_blocking_store_runs_on_its_own_threads():
    import threading

    seen: list[str] = []

    class _Recording(InMemoryUsageLedger):
        blocking = True

        def spent(self, key: str, day: str) -> int:
            seen.append(threading.current_thread().name)
            return 0

    runner, _ = _runner(TokenBudgetPlugin(per_user=10, ledger=_Recording()))
    assert await _turn(runner, "alice") == "ok"
    assert seen and all(name.startswith("orrery-token-budget") for name in seen)


def test_a_redis_store_gets_socket_timeouts(monkeypatch):
    import limits.storage

    calls: list[dict] = []
    real = limits.storage.storage_from_string

    def spy(uri, **options):
        calls.append(options)
        return real("memory://")

    monkeypatch.setattr(limits.storage, "storage_from_string", spy)
    StorageUsageLedger("redis://localhost:6379")
    assert calls[-1]["socket_timeout"] > 0 and calls[-1]["socket_connect_timeout"] > 0
    # Below the plugin's own wait, so a write it gave up on rarely lands later.
    assert calls[-1]["socket_timeout"] < mod_timeout()

    StorageUsageLedger("redis+sentinel://localhost:26379/primary")
    assert calls[-1]["sentinel_kwargs"]["socket_timeout"] > 0


def mod_timeout() -> float:
    from orrery_core.plugins import token_budget_plugin

    return token_budget_plugin._STORE_TIMEOUT_SECONDS
