"""Tests for the model-call retry policy and the fallback chain (AEP-021)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from orrery_core.agent.fallback import (
    FallbackLlm,
    LlmChainExhaustedError,
    resolve_model_chain,
    resolve_retry_options,
    should_fall_back,
    status_of,
)


class _HttpError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


class _LiteLlmStyleError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _FakeLlm(BaseLlm):
    """Fails with ``error`` (optionally after yielding once), else answers."""

    error: Any = None
    fail_after_yield: bool = False
    seen: list[LlmRequest] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        self.seen.append(llm_request)
        # Mutate the request the way a real provider may, so the test can tell
        # whether the next model got a pristine copy.
        llm_request.contents.append(types.Content(role="user", parts=[types.Part(text="x")]))
        if self.error is not None and not self.fail_after_yield:
            raise self.error
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=f"from {self.model}")])
        )
        if self.error is not None:
            raise self.error


def _fake(model: str, **kw: Any) -> _FakeLlm:
    return _FakeLlm(model=model, seen=[], **kw)


def _request() -> LlmRequest:
    return LlmRequest(
        model="agent-model",
        contents=[types.Content(role="user", parts=[types.Part(text="hello")])],
    )


async def _collect(llm: BaseLlm, request: LlmRequest) -> list[str]:
    return [
        part.text or ""
        async for response in llm.generate_content_async(request)
        for part in (response.content.parts if response.content else []) or []
    ]


# ── Error classification ─────────────────────────────────────────────


@pytest.mark.parametrize("code", [404, 408, 429, 500, 502, 503, 504])
def test_model_failures_move_the_chain(code):
    assert should_fall_back(_HttpError(code))
    assert should_fall_back(_LiteLlmStyleError(code))


@pytest.mark.parametrize("code", [400, 401, 403, 422])
def test_request_failures_do_not(code):
    """A bad request fails the same on every model; falling back only bills more."""
    assert not should_fall_back(_HttpError(code))


def test_status_is_read_through_the_cause_chain():
    try:
        try:
            raise _HttpError(503)
        except _HttpError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert status_of(outer) == 503
        assert should_fall_back(outer)


def test_timeouts_and_dropped_connections_fall_back():
    assert should_fall_back(TimeoutError())
    assert should_fall_back(ConnectionResetError())
    assert not should_fall_back(ValueError("bad schema"))


# ── The chain ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unavailable_primary_answers_on_the_fallback():
    primary = _fake("primary", error=_HttpError(503))
    backup = _fake("backup")
    chain = FallbackLlm(targets=[primary, backup])

    assert await _collect(chain, _request()) == ["from backup"]
    assert backup.seen[0].model == "backup"
    # The backup got the request the agent built, not the primary's leftovers.
    assert len(backup.seen[0].contents) == 2  # original + the backup's own append


@pytest.mark.asyncio
async def test_bad_request_is_raised_without_trying_another_model():
    primary = _fake("primary", error=_HttpError(400))
    backup = _fake("backup")
    with pytest.raises(_HttpError):
        await _collect(FallbackLlm(targets=[primary, backup]), _request())
    assert backup.seen == []


@pytest.mark.asyncio
async def test_never_switches_models_mid_answer():
    primary = _fake("primary", error=_HttpError(503), fail_after_yield=True)
    backup = _fake("backup")
    chain = FallbackLlm(targets=[primary, backup])
    got: list[str] = []
    with pytest.raises(_HttpError):
        async for response in chain.generate_content_async(_request()):
            parts = response.content.parts if response.content else None
            got.append((parts[0].text or "") if parts else "")
    assert got == ["from primary"]
    assert backup.seen == []


@pytest.mark.asyncio
async def test_every_model_failing_raises_chain_exhausted():
    chain = FallbackLlm(
        targets=[_fake("a", error=_HttpError(503)), _fake("b", error=_HttpError(429))]
    )
    with pytest.raises(LlmChainExhaustedError) as info:
        await _collect(chain, _request())
    assert isinstance(info.value.__cause__, _HttpError)


@pytest.mark.asyncio
async def test_a_failed_primary_is_tried_last_while_cooling_down():
    primary = _fake("primary", error=_HttpError(503))
    backup = _fake("backup")
    chain = FallbackLlm(targets=[primary, backup], cooldown_seconds=60)

    await _collect(chain, _request())
    await _collect(chain, _request())
    # Second call went straight to the backup: the dead primary was not paid for again.
    assert len(primary.seen) == 1
    assert len(backup.seen) == 2


@pytest.mark.asyncio
async def test_a_cooling_model_is_still_tried_when_nothing_else_works():
    primary = _fake("primary")
    chain = FallbackLlm(targets=[primary, _fake("backup", error=_HttpError(503))])
    chain._cooling_until[0] = float("inf")
    assert await _collect(chain, _request()) == ["from primary"]


def test_chain_reports_the_primarys_name_and_capabilities():
    primary = _fake("primary")
    chain = FallbackLlm(targets=[primary, _fake("backup")])
    assert chain.model == "primary"
    assert chain.capabilities == primary.capabilities


def test_chain_needs_two_models():
    with pytest.raises(ValueError):
        FallbackLlm(targets=[_fake("only")])


# ── Configuration ────────────────────────────────────────────────────


def test_no_chain_configured_returns_the_primary_unchanged(monkeypatch):
    monkeypatch.delenv("MODEL_FALLBACK_CHAIN", raising=False)
    assert resolve_model_chain("gemini-3.6-flash") == "gemini-3.6-flash"


def test_gemini_chain(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "gemini-3.6-flash-lite, gemini-flash-latest")
    chain = resolve_model_chain("gemini-3.6-pro")
    assert isinstance(chain, FallbackLlm)
    assert all(isinstance(t, Gemini) for t in chain.targets)
    assert [t.model for t in chain.targets] == [
        "gemini-3.6-pro",
        "gemini-3.6-flash-lite",
        "gemini-flash-latest",
    ]


def test_litellm_chain_prefixes_bare_names(monkeypatch):
    pytest.importorskip("litellm")
    from google.adk.models.lite_llm import LiteLlm

    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "claude-haiku-4-5,openai/gpt-5-mini")
    chain = resolve_model_chain(LiteLlm(model="anthropic/claude-sonnet-5"))
    assert isinstance(chain, FallbackLlm)
    assert [t.model for t in chain.targets] == [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-haiku-4-5",
        "openai/gpt-5-mini",
    ]


def test_mixing_families_is_refused_at_startup(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "anthropic/claude-sonnet-5")
    with pytest.raises(ValueError, match="mixes model families"):
        resolve_model_chain("gemini-3.6-flash")


def test_default_retry_policy(monkeypatch):
    for var in (
        "ORRERY_LLM_RETRY_ATTEMPTS",
        "ORRERY_LLM_RETRY_INITIAL_DELAY",
        "ORRERY_LLM_RETRY_MAX_DELAY",
    ):
        monkeypatch.delenv(var, raising=False)
    options = resolve_retry_options()
    assert options is not None
    assert options.attempts == 4
    assert 429 in (options.http_status_codes or [])
    assert 400 not in (options.http_status_codes or [])
    # A retired model (404) is the chain's job, not the retry's.
    assert 404 not in (options.http_status_codes or [])


@pytest.mark.parametrize("value", ["0", "1", "false", "off"])
def test_retry_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("ORRERY_LLM_RETRY_ATTEMPTS", value)
    assert resolve_retry_options() is None


def test_invalid_retry_setting_fails_loudly(monkeypatch):
    monkeypatch.setenv("ORRERY_LLM_RETRY_ATTEMPTS", "lots")
    with pytest.raises(ValueError, match="ORRERY_LLM_RETRY"):
        resolve_retry_options()


def test_agents_carry_the_retry_policy(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.delenv("MODEL_FALLBACK_CHAIN", raising=False)
    monkeypatch.setenv("ORRERY_LLM_RETRY_ATTEMPTS", "3")
    from orrery_core import create_agent

    agent = create_agent(name="t", description="d", instruction="i", tools=[])
    config = agent.generate_content_config
    assert config is not None
    assert config.http_options is not None
    assert config.http_options.retry_options is not None
    assert config.http_options.retry_options.attempts == 3
    # Safety filters are still attached alongside it on Gemini.
    assert config.safety_settings


def test_a_gemini_chain_keeps_the_safety_filters(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_SAFETY_FILTERS", raising=False)
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "gemini-flash-latest")
    from orrery_core import create_agent

    agent = create_agent(name="t", description="d", instruction="i", tools=[])
    assert isinstance(agent.model, FallbackLlm)
    assert agent.generate_content_config is not None
    assert agent.generate_content_config.safety_settings
