"""Per-user tokens for downstream systems: OAuth 2.0 Token Exchange (ADR-004).

Impersonation covers systems that accept an "on behalf of" header. Others take
a bearer token, and the caller's own token is the wrong one to send: it is
scoped to Orrery's audience, so a downstream system holding it could replay it
against Orrery. Instead the identity provider exchanges it (RFC 8693) for a
short-lived token with the target's own audience and scope.

**Where the subject token lives.** On the turn's :class:`CallerIdentity`, in
the ContextVar the gateway sets from the transport's verified token. Never in
session state, never visible to a tool or the model. Only callers with an
identity have one: a service principal acts for no one, and Slack, Google Chat
and the triage sweep hold no user token, so for them an exchanging target is
refused (fail-closed), not reached with the platform's own credentials.

**Cache.** Exchanged tokens are kept in memory, per subject token, audience and
scope, until shortly before they expire. Keyed by the subject token's digest
rather than the user: a new console token starts fresh, and nothing outlives
the token it was exchanged from by more than its own lifetime. Concurrent
requests for one key share a single exchange. Failures are remembered too, per
key: a refusal for 30 s, an IdP outage or misconfiguration with a backoff, so
a refused user doesn't cost an IdP round-trip on every model step.

**Off the shared executor.** Exchanges run on a small pool of their own: an
IdP brownout then holds those threads, not the default executor every
blocking tool (Kubernetes, Kafka, Docker) waits on.

**A misconfiguration is not a refusal.** ``invalid_client`` or a 401 means the
platform's own client is wrong (a rotated secret), for everyone. It is logged
at ERROR, audited, and answered as a failure the circuit breaker counts,
rather than as one user being refused.

Configuration (environment):

- ``ORRERY_TOKEN_EXCHANGE_URL``: the IdP token endpoint (https; http only for
  loopback);
- ``ORRERY_TOKEN_EXCHANGE_CLIENT_ID`` / ``ORRERY_TOKEN_EXCHANGE_CLIENT_SECRET``:
  the confidential client the IdP lets perform exchanges.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import requests

from ..observability.audit import audit_event
from .identity import current_caller

logger = logging.getLogger("orrery.token_exchange")

URL_ENV = "ORRERY_TOKEN_EXCHANGE_URL"
CLIENT_ID_ENV = "ORRERY_TOKEN_EXCHANGE_CLIENT_ID"
CLIENT_SECRET_ENV = "ORRERY_TOKEN_EXCHANGE_CLIENT_SECRET"  # noqa: S105 - an env var name

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105

#: An exchanged token is dropped this long before it expires, so a call never
#: starts with a token about to lapse mid-flight.
EXPIRY_SKEW_SECONDS = 30
#: Used when the IdP omits ``expires_in`` (it is only RECOMMENDED).
DEFAULT_LIFETIME_SECONDS = 300
MAX_CACHED_TOKENS = 1024
REQUEST_TIMEOUT_SECONDS = 10.0
#: Threads the exchange may hold; the rest of the process never waits on them.
EXCHANGE_WORKERS = 4
#: How long a refusal is remembered before the IdP is asked again.
REFUSAL_RETRY_SECONDS = 30.0
#: Backoff after an unavailable or misconfigured IdP: doubles per failure.
BACKOFF_INITIAL_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 60.0
#: RFC 6749 error codes that mean the platform's client is wrong, not the user.
_CLIENT_ERRORS = frozenset({"invalid_client", "unauthorized_client"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


Reason = Literal["no_caller", "refused", "unavailable", "misconfigured"]


class CredentialUnavailable(PermissionError):
    """No per-user token could be had for a target.

    ``reason`` says whose problem it is: ``no_caller`` and ``refused`` are an
    answer about this user (:attr:`is_refusal`); ``unavailable`` (the IdP is
    down) and ``misconfigured`` (the platform's client is wrong) are failures.
    """

    def __init__(self, message: str, *, reason: Reason = "refused") -> None:
        super().__init__(message)
        self.reason: Reason = reason

    @property
    def is_refusal(self) -> bool:
        return self.reason in ("no_caller", "refused")


@dataclass(frozen=True)
class ExchangeSettings:
    url: str
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> ExchangeSettings:
        """Read and check the settings; a missing or unsafe one fails startup."""
        url = os.getenv(URL_ENV, "").strip()
        client_id = os.getenv(CLIENT_ID_ENV, "").strip()
        secret = os.getenv(CLIENT_SECRET_ENV, "")
        missing = [
            name
            for name, value in (
                (URL_ENV, url),
                (CLIENT_ID_ENV, client_id),
                (CLIENT_SECRET_ENV, secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"token exchange needs {', '.join(missing)}")
        parts = urlsplit(url)
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOOPBACK):
            raise ValueError(f"{URL_ENV} must be https (http only for loopback)")
        return cls(url=url, client_id=client_id, client_secret=secret)


@dataclass(frozen=True)
class _Cached:
    token: str
    expires_at: float  # monotonic


@dataclass(frozen=True)
class _Failed:
    reason: Reason
    message: str
    until: float  # monotonic
    attempts: int


_Key = tuple[str, str, str]


class TokenBroker:
    """Exchanges the turn's subject token for per-target tokens, with a cache."""

    def __init__(
        self,
        settings: ExchangeSettings,
        *,
        session: requests.Session | None = None,
        max_entries: int = MAX_CACHED_TOKENS,
        clock: Any = time.monotonic,
    ) -> None:
        self._settings = settings
        self._session = session or requests.Session()
        self._max_entries = max_entries
        self._clock = clock
        self._cache: OrderedDict[_Key, _Cached] = OrderedDict()
        self._failures: OrderedDict[_Key, _Failed] = OrderedDict()
        # Keyed by event loop too: a task belongs to the loop that made it.
        self._inflight: dict[tuple[int, _Key], asyncio.Task[str]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=EXCHANGE_WORKERS, thread_name_prefix="orrery-token-exchange"
        )

    def close(self) -> None:
        """Release the exchange threads; an exchange in flight finishes first."""
        self._executor.shutdown(wait=False)

    async def token_for(self, audience: str, scope: str | None = None) -> str:
        """A token for *audience* acting as the turn's caller.

        Raises :class:`CredentialUnavailable` when there is no caller with a
        token, or the exchange fails (now, or recently enough to be remembered).
        """
        caller = current_caller()
        subject_token = caller.subject_token if caller else None
        if not subject_token:
            raise CredentialUnavailable(
                f"no signed-in user to act for: {audience} accepts only per-user "
                "tokens, which this channel cannot obtain",
                reason="no_caller",
            )
        key = (hashlib.sha256(subject_token.encode()).hexdigest(), audience, scope or "")
        now = self._clock()
        if (hit := self._cache.get(key)) and hit.expires_at > now:
            self._cache.move_to_end(key)
            return hit.token
        if (failed := self._failures.get(key)) and failed.until > now:
            raise CredentialUnavailable(failed.message, reason=failed.reason)
        loop = asyncio.get_running_loop()
        task = self._inflight.get((id(loop), key))
        if task is None:
            # Its own task, so a caller cancelled mid-exchange (a call deadline)
            # doesn't cancel it for the others waiting on the same key.
            task = loop.create_task(self._fetch(key, subject_token, audience, scope))
            self._inflight[(id(loop), key)] = task
        return await asyncio.shield(task)

    async def _fetch(self, key: _Key, subject_token: str, audience: str, scope: str | None) -> str:
        loop = asyncio.get_running_loop()
        try:
            token, lifetime = await loop.run_in_executor(
                self._executor,
                functools.partial(self._exchange, subject_token, audience, scope),
            )
        except CredentialUnavailable as exc:
            self._remember_failure(key, exc)
            raise
        finally:
            del self._inflight[(id(loop), key)]
        self._failures.pop(key, None)
        self._store(key, token, lifetime)
        return token

    def _remember_failure(self, key: _Key, exc: CredentialUnavailable) -> None:
        previous = self._failures.get(key)
        attempts = previous.attempts + 1 if previous else 1
        if exc.is_refusal:
            wait = REFUSAL_RETRY_SECONDS
        else:
            wait = min(BACKOFF_INITIAL_SECONDS * 2 ** (attempts - 1), BACKOFF_MAX_SECONDS)
        self._failures[key] = _Failed(exc.reason, str(exc), self._clock() + wait, attempts)
        self._failures.move_to_end(key)
        while len(self._failures) > self._max_entries:
            self._failures.popitem(last=False)

    def _store(self, key: _Key, token: str, lifetime: float) -> None:
        if lifetime <= 0:
            return
        # A token shorter-lived than the skew is still reused for half its life,
        # so a call doesn't exchange once to check and again to connect.
        usable = (
            lifetime - EXPIRY_SKEW_SECONDS if lifetime > 2 * EXPIRY_SKEW_SECONDS else lifetime / 2
        )
        self._cache[key] = _Cached(token, self._clock() + usable)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    def _exchange(self, subject_token: str, audience: str, scope: str | None) -> tuple[str, float]:
        form = {
            "grant_type": GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "audience": audience,
        }
        if scope:
            form["scope"] = scope
        try:
            response = self._session.post(
                self._settings.url,
                data=form,
                auth=(self._settings.client_id, self._settings.client_secret),
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.warning("Token exchange for %s failed: %s", audience, type(exc).__name__)
            raise CredentialUnavailable(
                f"the identity provider could not be reached for {audience}",
                reason="unavailable",
            ) from exc

        status = response.status_code
        if status >= 500 or status == 429:
            logger.warning("Token exchange for %s: IdP answered %s", audience, status)
            raise CredentialUnavailable(
                f"the identity provider is unavailable for {audience}", reason="unavailable"
            )
        body = _json(response)
        # RFC 6749 §5.2: `error` is a code, safe to log; the description may
        # echo request details, so it is not.
        error = body.get("error") if isinstance(body.get("error"), str) else None
        if status == 401 or error in _CLIENT_ERRORS:
            logger.error(
                "Token exchange is misconfigured: the IdP answered %s %s for client %s "
                "(check ORRERY_TOKEN_EXCHANGE_CLIENT_ID / _SECRET and the client's "
                "exchange permission)",
                status,
                error or "",
                self._settings.client_id,
            )
            audit_event(
                "token_exchange_misconfigured",
                audience=audience,
                status=status,
                error=error,
                client_id=self._settings.client_id,
            )
            raise CredentialUnavailable(
                "token exchange is misconfigured on this deployment", reason="misconfigured"
            )
        if status != 200:
            # A 403 is a refusal, but Keycloak also answers a client missing the
            # exchange permission with one, for everyone: louder than a 400.
            logger.log(
                logging.WARNING if status == 403 else logging.INFO,
                "Token exchange for %s refused: %s %s",
                audience,
                status,
                error or "unknown_error",
            )
            raise CredentialUnavailable(f"the identity provider refused a token for {audience}")
        token = body.get("access_token")
        # RFC 8693 §2.2.1: N_A marks a token that is *not* an access token, so
        # only a bearer access token is sent as one.
        if (
            not isinstance(token, str)
            or not token
            or str(body.get("token_type", "")).lower() != "bearer"
            or body.get("issued_token_type") != ACCESS_TOKEN_TYPE
        ):
            logger.warning("Token exchange for %s returned an unusable response", audience)
            raise CredentialUnavailable(
                f"the identity provider returned no usable token for {audience}"
            )
        expires_in = body.get("expires_in")
        lifetime = (
            float(expires_in)
            if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
            else DEFAULT_LIFETIME_SECONDS
        )
        return token, lifetime


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


_broker: TokenBroker | None = None


def default_broker() -> TokenBroker:
    """The process-wide broker, built from the environment on first use."""
    global _broker
    if _broker is None:
        _broker = TokenBroker(ExchangeSettings.from_env())
    return _broker


def reset_default_broker() -> None:
    """Forget the process-wide broker (tests, settings reload)."""
    global _broker
    if _broker is not None:
        _broker.close()
    _broker = None
