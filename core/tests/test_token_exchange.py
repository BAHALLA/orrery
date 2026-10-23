"""Token exchange (ADR-004): the broker, its cache, and where the token travels."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, cast

import jwt as pyjwt
import pytest
import requests

from orrery_core.security.auth import JWTConfig, verify_token
from orrery_core.security.identity import CallerIdentity, caller_scope, identity_from_claims
from orrery_core.security.token_exchange import (
    ACCESS_TOKEN_TYPE,
    GRANT_TYPE,
    CredentialUnavailable,
    ExchangeSettings,
    TokenBroker,
)

SUBJECT = "console-token-of-alice"
SETTINGS = ExchangeSettings(
    url="https://idp.example.com/token", client_id="orrery", client_secret="s3cret"
)


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeIdP:
    """Answers token exchanges; records each request."""

    def __init__(self, *answers: Any, delay: float = 0.0) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self.delay = delay
        self._lock = threading.Lock()

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        with self._lock:
            self.calls.append({"url": url, **kwargs})
        if self.delay:
            time.sleep(self.delay)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, FakeResponse):
            return answer
        form = kwargs["data"]
        return FakeResponse(
            200,
            {
                "access_token": f"x:{form['subject_token']}:{form['audience']}",
                "token_type": "Bearer",
                "issued_token_type": ACCESS_TOKEN_TYPE,
                "expires_in": answer,
            },
        )


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _broker(idp: FakeIdP, **kwargs: Any) -> TokenBroker:
    return TokenBroker(SETTINGS, session=cast(Any, idp), **kwargs)


def _as(token: str | None, subject: str = "alice"):
    return caller_scope(CallerIdentity(subject=subject, username=subject, subject_token=token))


# ── The exchange ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_exchange_is_rfc_8693_with_client_authentication():
    idp = FakeIdP(300)
    with _as(SUBJECT):
        token = await _broker(idp).token_for("github-mcp", "issues:read")

    assert token == f"x:{SUBJECT}:github-mcp"
    (call,) = idp.calls
    assert call["url"] == SETTINGS.url
    assert call["data"] == {
        "grant_type": GRANT_TYPE,
        "subject_token": SUBJECT,
        "subject_token_type": ACCESS_TOKEN_TYPE,
        "requested_token_type": ACCESS_TOKEN_TYPE,
        "audience": "github-mcp",
        "scope": "issues:read",
    }
    assert call["auth"] == ("orrery", "s3cret")
    assert call["allow_redirects"] is False
    assert call["timeout"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, ""])
async def test_no_signed_in_caller_means_no_token(token):
    idp = FakeIdP(300)
    with _as(token), pytest.raises(CredentialUnavailable, match="no signed-in user") as err:
        await _broker(idp).token_for("github-mcp")
    assert err.value.transient is False
    assert idp.calls == []


@pytest.mark.asyncio
async def test_outside_any_turn_there_is_no_token():
    with pytest.raises(CredentialUnavailable):
        await _broker(FakeIdP(300)).token_for("github-mcp")


@pytest.mark.asyncio
async def test_a_refusal_is_final_and_its_description_never_logged(caplog):
    idp = FakeIdP(
        FakeResponse(400, {"error": "invalid_grant", "error_description": f"bad {SUBJECT}"})
    )
    caplog.set_level(logging.DEBUG, logger="orrery.token_exchange")
    with _as(SUBJECT), pytest.raises(CredentialUnavailable, match="refused") as err:
        await _broker(idp).token_for("github-mcp")

    assert err.value.transient is False
    assert "invalid_grant" in caplog.text
    assert SUBJECT not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        FakeResponse(503, {}),
        FakeResponse(429, {}),
        requests.ConnectionError("refused"),
        requests.Timeout("slow"),
    ],
)
async def test_an_unavailable_idp_is_transient(answer):
    with _as(SUBJECT), pytest.raises(CredentialUnavailable) as err:
        await _broker(FakeIdP(answer)).token_for("github-mcp")
    assert err.value.transient is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"token_type": "Bearer"},  # no token
        {"access_token": "", "token_type": "Bearer"},
        {"access_token": "t", "token_type": "mac"},  # not a bearer token
        ValueError("not json"),
        ["a", "list"],
    ],
)
async def test_an_unusable_answer_is_refused(body):
    with _as(SUBJECT), pytest.raises(CredentialUnavailable):
        await _broker(FakeIdP(FakeResponse(200, body))).token_for("github-mcp")


# ── The cache ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tokens_are_cached_per_subject_token_audience_and_scope():
    idp = FakeIdP(300)
    broker = _broker(idp)
    with _as(SUBJECT):
        first = await broker.token_for("github-mcp")
        assert await broker.token_for("github-mcp") == first
        await broker.token_for("github-mcp", "issues:write")
        await broker.token_for("jira-mcp")
    with _as("console-token-of-bob", subject="bob"):
        assert await broker.token_for("github-mcp") != first

    assert len(idp.calls) == 4


@pytest.mark.asyncio
async def test_a_cached_token_is_dropped_before_it_expires():
    idp, clock = FakeIdP(300), Clock()
    broker = _broker(idp, clock=clock)
    with _as(SUBJECT):
        await broker.token_for("github-mcp")
        clock.now += 300 - 31
        await broker.token_for("github-mcp")
        assert len(idp.calls) == 1
        clock.now += 2  # inside the skew
        await broker.token_for("github-mcp")
    assert len(idp.calls) == 2


@pytest.mark.asyncio
async def test_a_token_too_short_lived_to_reuse_is_not_cached():
    idp = FakeIdP(10)
    broker = _broker(idp)
    with _as(SUBJECT):
        await broker.token_for("github-mcp")
        await broker.token_for("github-mcp")
    assert len(idp.calls) == 2


@pytest.mark.asyncio
async def test_the_cache_is_bounded():
    idp = FakeIdP(300)
    broker = _broker(idp, max_entries=2)
    for user in ("a", "b", "c"):
        with _as(f"token-{user}", subject=user):
            await broker.token_for("github-mcp")
    with _as("token-a", subject="a"):
        await broker.token_for("github-mcp")  # evicted: exchanged again
    assert len(idp.calls) == 4


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_exchange():
    idp = FakeIdP(300, delay=0.05)
    broker = _broker(idp)
    with _as(SUBJECT):
        tokens = await asyncio.gather(*(broker.token_for("github-mcp") for _ in range(5)))
    assert len(set(tokens)) == 1
    assert len(idp.calls) == 1


@pytest.mark.asyncio
async def test_a_cancelled_caller_does_not_cancel_the_exchange_for_others():
    idp = FakeIdP(300, delay=0.1)
    broker = _broker(idp)
    with _as(SUBJECT):
        first = asyncio.create_task(broker.token_for("github-mcp"))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(broker.token_for("github-mcp"))
        await asyncio.sleep(0.01)
        first.cancel()
        assert await second == f"x:{SUBJECT}:github-mcp"
    assert len(idp.calls) == 1


# ── Settings ─────────────────────────────────────────────────────────


@pytest.fixture
def exchange_env(monkeypatch):
    monkeypatch.setenv("ORRERY_TOKEN_EXCHANGE_URL", "https://idp.example.com/token")
    monkeypatch.setenv("ORRERY_TOKEN_EXCHANGE_CLIENT_ID", "orrery")
    monkeypatch.setenv("ORRERY_TOKEN_EXCHANGE_CLIENT_SECRET", "s3cret")
    return monkeypatch


def test_settings_come_from_the_environment(exchange_env):
    assert ExchangeSettings.from_env() == SETTINGS


@pytest.mark.parametrize(
    "var",
    [
        "ORRERY_TOKEN_EXCHANGE_URL",
        "ORRERY_TOKEN_EXCHANGE_CLIENT_ID",
        "ORRERY_TOKEN_EXCHANGE_CLIENT_SECRET",
    ],
)
def test_a_missing_setting_fails_startup(exchange_env, var):
    exchange_env.delenv(var)
    with pytest.raises(ValueError, match=var):
        ExchangeSettings.from_env()


@pytest.mark.parametrize(
    ("url", "ok"),
    [("http://idp.example.com/token", False), ("http://localhost:8080/token", True)],
)
def test_the_token_endpoint_is_https_except_on_loopback(exchange_env, url, ok):
    exchange_env.setenv("ORRERY_TOKEN_EXCHANGE_URL", url)
    if ok:
        assert ExchangeSettings.from_env().url == url
    else:
        with pytest.raises(ValueError, match="https"):
            ExchangeSettings.from_env()


# ── Where the subject token travels ──────────────────────────────────


def test_the_verified_token_rides_the_auth_context_but_never_its_state():
    secret = "k" * 32
    claims = {"sub": "alice", "email": "a@example.com", "exp": int(time.time()) + 60}
    raw = pyjwt.encode(claims, secret, algorithm="HS256")

    auth = verify_token(raw, JWTConfig(algorithm="HS256", secret=secret))

    assert auth.token == raw
    assert raw not in repr(auth)
    assert raw not in str(auth.as_state())


def test_the_caller_identity_carries_it_out_of_repr_and_equality():
    identity = identity_from_claims("alice", {"email": "a@example.com"}, subject_token=SUBJECT)

    assert identity.subject_token == SUBJECT
    assert SUBJECT not in repr(identity)
    assert identity == identity_from_claims("alice", {"email": "a@example.com"})
