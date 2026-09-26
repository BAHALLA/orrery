"""Readiness: can this pod serve a request *right now*?

``/readyz`` used to answer ``ready`` unconditionally. So a pod whose session
database had gone away kept receiving traffic and failed every request, and
a rolling update could send traffic to a new pod before it could reach its
database.

Readiness checks only this server's **own hard dependency**, the session store
(which also backs pending approvals when ``ORRERY_CONFIRMATION_BACKEND=postgres``).
It deliberately does not check the model provider or the integrations (Kafka,
Kubernetes, ...): those are what the agent diagnoses. A broken Kafka is a
question to answer, not a reason to stop answering questions, and
``/onboarding/selftest`` reports on them. Liveness (``/healthz``) checks
nothing external, so a database outage never restarts pods.

The probe is cheap and bounded: one short-lived connection with a 2 s
timeout, run off the event loop, and cached for a few seconds. With a result
cached and one check in flight at a time, a probe storm cannot pile up
database connections.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger("orrery.readiness")

#: Seconds a readiness result is reused.
CACHE_SECONDS = 5.0
#: Seconds to wait for the database connection.
CONNECT_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    checks: dict[str, str]


class ReadinessCheck:
    """Cached, single-flight readiness of the session database.

    Args:
        database_url: The session store URL, or ``None`` for in-memory sessions
            (always ready: there is nothing external to lose).
        probe: ``(url, connect_timeout) -> bool``; defaults to
            :func:`orrery_core.persistence.db.database_reachable`.
        clock: Monotonic clock; injectable for tests.
    """

    def __init__(
        self,
        database_url: str | None,
        *,
        probe: Callable[..., bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = database_url
        self._probe = probe
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached: ReadinessResult | None = None
        self._cached_at = 0.0

    async def check(self) -> ReadinessResult:
        if self._url is None:
            return ReadinessResult(ready=True, checks={"database": "not_configured"})
        async with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._cached_at < CACHE_SECONDS:
                return self._cached
            reachable = await asyncio.to_thread(self._reachable)
            result = ReadinessResult(
                ready=reachable, checks={"database": "ok" if reachable else "unreachable"}
            )
            if self._cached is not None and self._cached.ready != result.ready:
                logger.warning("Readiness changed: database %s", result.checks["database"])
            self._cached, self._cached_at = result, now
            return result

    def _reachable(self) -> bool:
        assert self._url is not None  # noqa: S101 — checked by the caller
        probe = self._probe
        if probe is None:
            from ..persistence.db import database_reachable

            probe = database_reachable
        try:
            return bool(probe(self._url, connect_timeout=CONNECT_TIMEOUT_SECONDS))
        except Exception as exc:  # a probe must answer, never raise
            logger.warning("Readiness probe failed: %s", type(exc).__name__)
            return False
