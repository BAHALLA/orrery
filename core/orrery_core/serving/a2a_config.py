"""``ORRERY_A2A_*`` settings, readable without the ``[a2a]`` extra installed.

The server reads them on every start to decide whether to mount A2A at all;
the a2a-sdk is imported only when it does (:mod:`.a2a_server`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..env import env_flag, env_int
from .rate_limit import parse_rate

A2A_ENABLED_ENV = "ORRERY_A2A_ENABLED"
A2A_AUDIENCE_ENV = "ORRERY_A2A_JWT_AUDIENCE"
A2A_PUBLIC_URL_ENV = "ORRERY_A2A_PUBLIC_URL"
A2A_APPROVAL_BASE_URL_ENV = "ORRERY_A2A_APPROVAL_BASE_URL"
A2A_RATE_LIMIT_ENV = "ORRERY_A2A_RATE_LIMIT"
A2A_MAX_TASKS_IN_FLIGHT_ENV = "ORRERY_A2A_MAX_TASKS_IN_FLIGHT"
A2A_MAX_MESSAGE_CHARS_ENV = "ORRERY_A2A_MAX_MESSAGE_CHARS"
A2A_MAX_BODY_BYTES_ENV = "ORRERY_A2A_MAX_BODY_BYTES"
A2A_MAX_TASKS_ENV = "ORRERY_A2A_MAX_TASKS"
A2A_TASK_IDLE_TTL_ENV = "ORRERY_A2A_TASK_IDLE_TTL_SECONDS"

#: The console's message bound (``uploads._MAX_MESSAGE_CHARS``), applied to A2A.
DEFAULT_MAX_MESSAGE_CHARS = 8192

#: Where the JSON-RPC endpoint and its card live on the HTTP app.
A2A_PATH = "/a2a"
#: Set by Orrery on every A2A call it makes. A call carrying it came from an
#: Orrery deployment, possibly this one through a remote agent: refused, so
#: two agents cannot bounce a task between them at the platform's model cost.
HOP_HEADER = "x-orrery-hop"
#: Data-part ``kind`` of the pending-action payload on ``input-required``.
PENDING_ACTION_KIND = "orrery.pending_action"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class A2AConfig:
    """``ORRERY_A2A_*``: whether and how Orrery answers A2A calls."""

    enabled: bool = False
    #: The ``aud`` every A2A token must carry. Required: a console token is not
    #: an agent's credential, and accepting one would let any browser session
    #: be replayed as an agent.
    audience: str | None = None
    #: The JSON-RPC URL advertised on the card, as clients reach it.
    public_url: str = "http://127.0.0.1:8000" + A2A_PATH
    #: Where a person approves: ``{base}/confirmations/{id}/decision``. Defaults
    #: to the origin of :attr:`public_url`.
    approval_base_url: str | None = None
    rate_limit: str = "30/minute"
    #: Turns one caller may have running at once. Each one buys model tokens.
    max_tasks_in_flight: int = 2
    #: Characters one message may carry; a longer one is rejected.
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS
    #: Bytes one JSON-RPC request body may carry, checked before it is parsed.
    #: A maximal message fits with its JSON-RPC framing even fully escaped
    #: (8192 chars, at worst 12 bytes each as a ``\uXXXX`` surrogate pair).
    max_body_bytes: int = 128 * 1024
    #: Tasks the in-memory store keeps; past it the least recently touched go.
    max_tasks: int = 10_000
    #: Seconds an untouched task is kept.
    task_idle_ttl_seconds: float = 3600.0

    @classmethod
    def from_env(cls) -> A2AConfig:
        return cls(
            enabled=env_flag(A2A_ENABLED_ENV, default=False),
            audience=os.getenv(A2A_AUDIENCE_ENV, "").strip() or None,
            public_url=os.getenv(A2A_PUBLIC_URL_ENV, "").strip().rstrip("/") or cls.public_url,
            approval_base_url=os.getenv(A2A_APPROVAL_BASE_URL_ENV, "").strip().rstrip("/") or None,
            rate_limit=os.getenv(A2A_RATE_LIMIT_ENV, "").strip() or cls.rate_limit,
            max_tasks_in_flight=env_int(A2A_MAX_TASKS_IN_FLIGHT_ENV, 2),
            max_message_chars=env_int(A2A_MAX_MESSAGE_CHARS_ENV, DEFAULT_MAX_MESSAGE_CHARS),
            max_body_bytes=env_int(A2A_MAX_BODY_BYTES_ENV, cls.max_body_bytes),
            max_tasks=env_int(A2A_MAX_TASKS_ENV, cls.max_tasks),
            task_idle_ttl_seconds=float(
                env_int(A2A_TASK_IDLE_TTL_ENV, int(cls.task_idle_ttl_seconds))
            ),
        )

    @property
    def approval_base(self) -> str:
        if self.approval_base_url:
            return self.approval_base_url
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    def validate(self, *, auth_enabled: bool) -> None:
        """Fail startup on a configuration that would expose agents unsafely."""
        if not auth_enabled:
            raise ValueError(
                f"{A2A_ENABLED_ENV} needs AUTH_ENABLED=true: an anonymous agent "
                "endpoint is an open door to every tool behind it."
            )
        if not self.audience:
            raise ValueError(
                f"{A2A_ENABLED_ENV} needs {A2A_AUDIENCE_ENV}: the audience agent "
                "tokens are issued for, distinct from the console's."
            )
        for env, value in (
            (A2A_MAX_TASKS_IN_FLIGHT_ENV, self.max_tasks_in_flight),
            (A2A_MAX_MESSAGE_CHARS_ENV, self.max_message_chars),
            (A2A_MAX_BODY_BYTES_ENV, self.max_body_bytes),
            (A2A_MAX_TASKS_ENV, self.max_tasks),
            (A2A_TASK_IDLE_TTL_ENV, self.task_idle_ttl_seconds),
        ):
            if value < 1:
                raise ValueError(f"{env} must be at least 1")
        parse_rate(self.rate_limit)  # a malformed limit fails here, not per request
        for name, url in (
            (A2A_PUBLIC_URL_ENV, self.public_url),
            (A2A_APPROVAL_BASE_URL_ENV, self.approval_base),
        ):
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError(f"{name}: {url!r} is not an http(s) URL")
            if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
                raise ValueError(f"{name}: {url!r} must use https outside loopback")
