"""Per-key request rate limiting for the HTTP transports.

A thin layer over `limits <https://limits.readthedocs.io>`_ — the library that
``slowapi`` itself wraps. ``slowapi`` was dropped because (a) its last release
still calls ``asyncio.iscoroutinefunction``, which Python 3.16 removes, and (b)
its decorator API keys the limit off the ``Request`` *before* FastAPI resolves
dependencies, which is why the old code had to smuggle the verified subject
through ``request.state``. Here a limit is an ordinary FastAPI dependency that
can itself depend on authentication, so the key is the verified identity by
construction.

Usage::

    limiter = RateLimiter("memory://")          # or redis://host:6379
    install_rate_limit_handler(app)

    async def per_user(auth: AuthContext = Depends(auth_dependency)) -> None:
        limiter.hit(chat_limit, f"sub:{auth.subject}")

    @app.post("/chat", dependencies=[Depends(per_user)])

The storage call is synchronous, as it was under ``slowapi``: in-memory it is a
dict update, and against Redis it is a single round-trip per request.
"""

from __future__ import annotations

import math
import time

from limits import RateLimitItem, parse
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover — covered by the install-extra path
    raise ImportError(
        "orrery_core.serving.rate_limit requires FastAPI. Install with: uv sync --extra server"
    ) from exc

__all__ = [
    "RateLimitExceeded",
    "RateLimiter",
    "install_rate_limit_handler",
    "parse_rate",
]


def parse_rate(rate: str) -> RateLimitItem:
    """Parse ``"30/minute"``-style notation, failing loudly on a typo.

    Called at startup for every configured limit, so a malformed
    ``ORRERY_CHAT_RATE_LIMIT`` stops the process instead of surfacing as a 500
    on the first request.
    """
    return parse(rate)


class RateLimitExceeded(Exception):
    """A key spent its budget for the current window."""

    def __init__(self, limit: RateLimitItem, retry_after: int) -> None:
        super().__init__(f"{limit} exceeded")
        self.limit = limit
        self.retry_after = retry_after


class RateLimiter:
    """Fixed-window counters keyed by caller, in memory or a shared store.

    ``memory://`` counts per process — with N replicas a caller gets N times
    the limit; point ``storage_uri`` at Redis to share counters. An unknown
    scheme raises ``limits.errors.ConfigurationError`` here, at startup.
    """

    def __init__(self, storage_uri: str = "memory://") -> None:
        self.storage_uri = storage_uri
        self._strategy = FixedWindowRateLimiter(storage_from_string(storage_uri))

    def hit(self, limit: RateLimitItem, key: str) -> None:
        """Charge one request to *key*; raise :class:`RateLimitExceeded` if over."""
        if self._strategy.hit(limit, key):
            return
        reset_at = self._strategy.get_window_stats(limit, key).reset_time
        raise RateLimitExceeded(limit, retry_after=max(1, math.ceil(reset_at - time.time())))


def _rate_limited(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RateLimitExceeded)  # nosec B101 — registered for this type only
    return JSONResponse(
        status_code=429,
        content={"error": "rate_limited", "detail": str(exc)},
        # The web console turns this into "retry in Ns"; Slack backs off on it.
        headers={"Retry-After": str(exc.retry_after)},
    )


def install_rate_limit_handler(app: FastAPI) -> None:
    """Render :class:`RateLimitExceeded` as ``429 {"error": "rate_limited"}``."""
    app.add_exception_handler(RateLimitExceeded, _rate_limited)
