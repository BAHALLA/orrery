"""Tests for orrery_core.serving.rate_limit (the slowapi replacement)."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from limits.errors import ConfigurationError

from orrery_core.serving.rate_limit import (
    RateLimiter,
    RateLimitExceeded,
    install_rate_limit_handler,
    parse_rate,
)


def test_hit_allows_up_to_the_limit_then_raises():
    limiter = RateLimiter()
    limit = parse_rate("2/minute")

    limiter.hit(limit, "alice")
    limiter.hit(limit, "alice")
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.hit(limit, "alice")

    # Whole seconds until the window resets, never 0 — "retry in 0s" is a lie
    # that turns into an immediate second 429.
    assert 1 <= exc.value.retry_after <= 60


def test_keys_are_counted_independently():
    limiter = RateLimiter()
    limit = parse_rate("1/minute")

    limiter.hit(limit, "alice")
    limiter.hit(limit, "bob")  # does not raise: bob has his own bucket


def test_limits_are_counted_independently():
    """One key spending its /chat budget must not drain its self-test budget."""
    limiter = RateLimiter()

    limiter.hit(parse_rate("1/minute"), "alice")
    limiter.hit(parse_rate("1/hour"), "alice")


def test_malformed_rate_fails_at_parse_time():
    with pytest.raises(ValueError):
        parse_rate("thirty a minute")


def test_unknown_storage_fails_at_construction():
    with pytest.raises(ConfigurationError, match="unknown storage scheme"):
        RateLimiter("bogus://nowhere")


def test_handler_renders_429_with_retry_after():
    app = FastAPI()
    limiter = RateLimiter()
    limit = parse_rate("1/minute")
    install_rate_limit_handler(app)

    async def charge() -> None:
        limiter.hit(limit, "k")

    @app.get("/x", dependencies=[Depends(charge)])
    async def x():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/x").status_code == 200

    blocked = client.get("/x")
    assert blocked.status_code == 429
    assert blocked.json() == {"error": "rate_limited", "detail": "1 per 1 minute exceeded"}
    assert int(blocked.headers["Retry-After"]) >= 1
