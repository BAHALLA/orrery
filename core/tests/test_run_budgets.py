"""Tests for the per-run budgets: calls, delegations, repeated failures, output bytes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from orrery_core.plugins import (
    CALL_BUDGET_STATUS,
    DELEGATION_REFUSED_STATUS,
    REPEAT_REFUSED_STATUS,
    RUN_OUTPUT_BUDGET_STATUS,
    CallBudgetPlugin,
    DelegationGuardPlugin,
    RepeatGuardPlugin,
    ToolOutputCapPlugin,
    default_plugins,
)
from orrery_core.plugins.repeat_guard_plugin import error_signature, fingerprint
from orrery_core.plugins.run_scope import RunLedger, invocation_id, run_key, session_id


async def lookup(name: str) -> dict:
    return {"status": "success", "name": name}


def _ctx(run: str = "run-1", session: str = "s-1", agent: str = "") -> Any:
    ctx = MagicMock()
    ctx.invocation_id = run
    ctx.agent_name = agent
    ctx.session.id = session
    return ctx


def _tool(name: str = "lookup") -> Any:
    tool = MagicMock()
    tool.name = name
    return tool


# ── run_scope ────────────────────────────────────────────────────────


def test_run_ledger_is_bounded_lru():
    ledger: RunLedger[list[int]] = RunLedger(lambda: [0], size=2)
    ledger.get("a")[0] = 1
    ledger.get("b")
    ledger.get("a")  # touch a: b is now the oldest
    ledger.get("c")
    assert ledger.peek("b") is None
    assert ledger.peek("a") == [1]
    assert len(ledger) == 2


def test_ids_are_read_from_the_context_or_its_invocation():
    assert invocation_id(_ctx(run="r")) == "r"
    assert session_id(_ctx(session="s")) == "s"
    inner = MagicMock(spec=["_invocation_context"])
    inner._invocation_context.invocation_id = "deep"
    inner._invocation_context.session.id = "deep-s"
    assert invocation_id(inner) == "deep"
    assert session_id(inner) == "deep-s"


def test_run_key_separates_the_nodes_of_one_invocation():
    """Graph Workflow nodes share the parent's invocation id; each is its own run."""
    assert run_key(_ctx(run="inv", agent="kafka_checker")) == "inv/kafka_checker"
    assert run_key(_ctx(run="inv", agent="k8s_checker")) == "inv/k8s_checker"
    assert run_key(_ctx(run="", agent="x")) == ""


# ── CallBudgetPlugin ─────────────────────────────────────────────────


async def _spend(plugin: CallBudgetPlugin, ctx: Any, n: int, status: str = "success") -> None:
    for _ in range(n):
        assert (
            await plugin.before_tool_callback(tool=_tool(), tool_args={}, tool_context=ctx) is None
        )
        await plugin.after_tool_callback(
            tool=_tool(), tool_args={}, tool_context=ctx, result={"status": status}
        )


@pytest.mark.asyncio
async def test_call_budget_refuses_past_the_limit_and_keeps_refusing():
    plugin = CallBudgetPlugin(max_calls=3)
    ctx = _ctx()
    await _spend(plugin, ctx, 3)
    for _ in range(2):
        refusal = await plugin.before_tool_callback(tool=_tool(), tool_args={}, tool_context=ctx)
        assert refusal is not None and refusal["status"] == CALL_BUDGET_STATUS
        await plugin.after_tool_callback(
            tool=_tool(), tool_args={}, tool_context=ctx, result=refusal
        )
    assert plugin.spent("run-1") == 3  # refusals are not charged


@pytest.mark.asyncio
async def test_call_budget_is_per_run():
    plugin = CallBudgetPlugin(max_calls=2)
    await _spend(plugin, _ctx(run="root"), 2)
    # A specialist's own invocation has its own allowance.
    assert (
        await plugin.before_tool_callback(tool=_tool(), tool_args={}, tool_context=_ctx("sub"))
        is None
    )


@pytest.mark.asyncio
async def test_call_budget_is_per_workflow_node():
    plugin = CallBudgetPlugin(max_calls=2)
    await _spend(plugin, _ctx(run="sweep", agent="kafka_checker"), 2)
    assert (
        await plugin.before_tool_callback(
            tool=_tool(), tool_args={}, tool_context=_ctx(run="sweep", agent="k8s_checker")
        )
        is None
    )


@pytest.mark.asyncio
async def test_gate_refusals_do_not_spend_the_budget():
    plugin = CallBudgetPlugin(max_calls=2)
    ctx = _ctx()
    await _spend(plugin, ctx, 5, status="access_denied")
    assert plugin.spent("run-1") == 0


@pytest.mark.asyncio
async def test_failed_calls_do_spend_it():
    plugin = CallBudgetPlugin(max_calls=2)
    await _spend(plugin, _ctx(), 2, status="error")
    assert plugin.spent("run-1") == 2


@pytest.mark.asyncio
async def test_model_is_warned_before_the_budget_is_spent():
    plugin = CallBudgetPlugin(max_calls=10, warn_fraction=0.7)
    ctx = _ctx()
    request = LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="q")])])

    await _spend(plugin, ctx, 6)
    await plugin.before_model_callback(callback_context=ctx, llm_request=request)
    assert len(request.contents) == 1

    await _spend(plugin, ctx, 1)
    await plugin.before_model_callback(callback_context=ctx, llm_request=request)
    assert len(request.contents) == 2
    parts = request.contents[-1].parts or []
    note = parts[0].text if parts else None
    assert note is not None and "7 of 10" in note and "3 left" in note


# ── DelegationGuardPlugin ────────────────────────────────────────────


def _agent_tool(name: str = "k8s_specialist") -> AgentTool:
    return AgentTool(agent=LlmAgent(name=name, model="gemini-3.6-flash", instruction="i"))


@pytest.mark.asyncio
async def test_delegation_guard_counts_one_specialist_per_run():
    plugin = DelegationGuardPlugin(max_per_agent=2)
    k8s, kafka = _agent_tool("k8s_specialist"), _agent_tool("kafka_specialist")
    ctx = _ctx()
    for _ in range(2):
        assert await plugin.before_tool_callback(tool=k8s, tool_args={}, tool_context=ctx) is None
    refusal = await plugin.before_tool_callback(tool=k8s, tool_args={}, tool_context=ctx)
    assert refusal is not None and refusal["status"] == DELEGATION_REFUSED_STATUS
    assert "tell the user" in refusal["error"]
    # Another specialist, and the same one in a new turn, are untouched.
    assert await plugin.before_tool_callback(tool=kafka, tool_args={}, tool_context=ctx) is None
    assert (
        await plugin.before_tool_callback(tool=k8s, tool_args={}, tool_context=_ctx("next")) is None
    )


@pytest.mark.asyncio
async def test_delegation_guard_ignores_ordinary_tools():
    plugin = DelegationGuardPlugin(max_per_agent=1)
    tool = FunctionTool(lookup)
    for _ in range(5):
        assert (
            await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=_ctx()) is None
        )


# ── RepeatGuardPlugin ────────────────────────────────────────────────


async def _attempt(plugin: RepeatGuardPlugin, args: dict, result: dict, ctx: Any = None) -> Any:
    ctx = ctx or _ctx()
    refusal = await plugin.before_tool_callback(tool=_tool(), tool_args=args, tool_context=ctx)
    await plugin.after_tool_callback(
        tool=_tool(), tool_args=args, tool_context=ctx, result=refusal or result
    )
    return refusal


_FAIL = {"status": "error", "error": "namespace 'paymnts' not found (request 81723)"}


@pytest.mark.asyncio
async def test_third_identical_failure_is_refused():
    plugin = RepeatGuardPlugin(max_failures=2)
    args = {"namespace": "paymnts"}
    assert await _attempt(plugin, args, _FAIL) is None
    assert await _attempt(plugin, args, _FAIL) is None
    refusal = await _attempt(plugin, args, _FAIL)
    assert refusal is not None and refusal["status"] == REPEAT_REFUSED_STATUS
    assert "ask them" in refusal["error"]


@pytest.mark.asyncio
async def test_a_probe_that_fails_again_closes_it_for_the_session():
    plugin = RepeatGuardPlugin(max_failures=2)
    args = {"namespace": "paymnts"}
    await _attempt(plugin, args, _FAIL)
    await _attempt(plugin, args, _FAIL)
    assert await _attempt(plugin, args, _FAIL) is not None  # refused, now half-open
    assert await _attempt(plugin, args, _FAIL) is None  # the probe runs... and fails
    assert await _attempt(plugin, args, _FAIL) is not None
    assert await _attempt(plugin, args, _FAIL) is not None  # closed: no second probe


@pytest.mark.asyncio
async def test_a_probe_that_succeeds_reopens_it():
    plugin = RepeatGuardPlugin(max_failures=2)
    args = {"namespace": "payments"}
    await _attempt(plugin, args, _FAIL)
    await _attempt(plugin, args, _FAIL)
    assert await _attempt(plugin, args, _FAIL) is not None
    assert await _attempt(plugin, args, {"status": "success"}) is None  # probe: fixed now
    assert await _attempt(plugin, args, _FAIL) is None  # counting from zero again


@pytest.mark.asyncio
async def test_changed_arguments_or_a_new_error_are_progress():
    plugin = RepeatGuardPlugin(max_failures=2)
    await _attempt(plugin, {"namespace": "a"}, _FAIL)
    await _attempt(plugin, {"namespace": "a"}, _FAIL)
    assert await _attempt(plugin, {"namespace": "b"}, _FAIL) is None  # different call

    other = {"status": "error", "error": "permission denied"}
    plugin = RepeatGuardPlugin(max_failures=2)
    await _attempt(plugin, {"namespace": "a"}, _FAIL)
    await _attempt(plugin, {"namespace": "a"}, other)  # new error restarts the count
    assert await _attempt(plugin, {"namespace": "a"}, other) is None


@pytest.mark.asyncio
async def test_repeat_guard_is_per_session_and_forgets_successes():
    plugin = RepeatGuardPlugin(max_failures=2)
    args = {"namespace": "x"}
    await _attempt(plugin, args, _FAIL, _ctx(session="s1"))
    await _attempt(plugin, args, _FAIL, _ctx(session="s1"))
    assert await _attempt(plugin, args, _FAIL, _ctx(session="s2")) is None
    await _attempt(plugin, {"namespace": "ok"}, {"status": "success"}, _ctx(session="s3"))
    assert plugin._sessions.get("s3") == {}  # successes are never stored


def test_error_signature_ignores_ids_and_whitespace():
    a = error_signature({"error": "Timeout after 3001 ms  (req 1234)"})
    b = error_signature({"error": "timeout after 2998 ms (req 99)"})
    assert a == b
    assert fingerprint("t", {"b": 1, "a": 2}) == fingerprint("t", {"a": 2, "b": 1})


# ── ToolOutputCapPlugin run budget ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_output_budget_shrinks_then_replaces_results():
    plugin = ToolOutputCapPlugin(max_bytes=1000, max_run_bytes=1500)
    ctx = _ctx()
    big = {"status": "success", "logs": "x" * 900}

    assert (
        await plugin.after_tool_callback(tool=_tool(), tool_args={}, tool_context=ctx, result=big)
        is None
    )
    capped = await plugin.after_tool_callback(
        tool=_tool(), tool_args={}, tool_context=ctx, result=big
    )
    assert capped is not None and capped.get("_truncated")  # cut to what was left
    spent = await plugin.after_tool_callback(
        tool=_tool(), tool_args={}, tool_context=ctx, result={"status": "success"}
    )
    assert spent is not None and spent["status"] == RUN_OUTPUT_BUDGET_STATUS
    # A new run starts with a full budget.
    assert (
        await plugin.after_tool_callback(
            tool=_tool(), tool_args={}, tool_context=_ctx("run-2"), result=big
        )
        is None
    )


@pytest.mark.asyncio
async def test_gate_answers_pass_through_a_spent_run_budget():
    plugin = ToolOutputCapPlugin(max_bytes=0, max_run_bytes=10)
    ctx = _ctx()
    await plugin.after_tool_callback(
        tool=_tool(), tool_args={}, tool_context=ctx, result={"status": "success", "x": "y" * 50}
    )
    gate = {"status": "confirmation_required", "message": "approve?"}
    assert (
        await plugin.after_tool_callback(tool=_tool(), tool_args={}, tool_context=ctx, result=gate)
        is None
    )


# ── End to end through default_plugins ───────────────────────────────


class _LoopingLlm(BaseLlm):
    """Calls ``lookup`` until a call is refused, then answers. Records requests."""

    requests: list[LlmRequest] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.requests.append(llm_request.model_copy(deep=True))
        refused = any(
            part.function_response
            and (part.function_response.response or {}).get("status") == CALL_BUDGET_STATUS
            for content in llm_request.contents
            for part in content.parts or []
        )
        if refused:
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part.from_text(text="done")])
            )
            return
        n = len(self.requests)
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_function_call(name="lookup", args={"name": f"q{n}"})],
            )
        )


@pytest.fixture(autouse=True)
def _no_ambient_provider_env(monkeypatch):
    for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_a_runaway_search_ends_with_an_answer():
    llm = _LoopingLlm(model="scripted", requests=[])
    agent = LlmAgent(name="searcher", model=llm, tools=[lookup])
    sessions = InMemorySessionService()
    plugins = default_plugins(enable_tracing=False, max_tool_calls_per_run=4)
    runner = Runner(
        app=App(name="budget_e2e", root_agent=agent, plugins=plugins), session_service=sessions
    )
    session = await sessions.create_session(app_name="budget_e2e", user_id="u")

    statuses: list[str] = []
    final = ""
    async for event in runner.run_async(
        user_id="u",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text="find it")]),
    ):
        for part in (event.content.parts if event.content else None) or []:
            if part.function_response:
                statuses.append(str((part.function_response.response or {}).get("status")))
            if part.text:
                final = part.text

    assert statuses == ["success"] * 4 + [CALL_BUDGET_STATUS]
    assert final == "done"
    # The model was warned before it hit the wall (70% of 4 → from the 2nd call).
    warned = [
        r
        for r in llm.requests
        if any("Tool-call budget" in (p.text or "") for c in r.contents for p in c.parts or [])
    ]
    assert warned
