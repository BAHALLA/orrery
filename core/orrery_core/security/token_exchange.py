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
requests for one key share a single exchange.

Configuration (environment):

- ``ORRERY_TOKEN_EXCHANGE_URL``: the IdP token endpoint (https; http only for
  loopback);
- ``ORRERY_TOKEN_EXCHANGE_CLIENT_ID`` / ``ORRERY_TOKEN_EXCHANGE_CLIENT_SECRET``:
  the confidential client the IdP lets perform exchanges.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

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
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class CredentialUnavailable(PermissionError):
    """No per-user token could be had for a target.

    ``transient`` is True when trying again may help (the IdP is unreachable or
    erroring); False when the answer is no (no caller, or the IdP refused).
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


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
        self._cache: OrderedDict[tuple[str, str, str], _Cached] = OrderedDict()
        self._inflight: dict[tuple[str, str, str], asyncio.Task[str]] = {}

    async def token_for(self, audience: str, scope: str | None = None) -> str:
        """A token for *audience* acting as the turn's caller.

        Raises :class:`CredentialUnavailable` when there is no caller with a
        token, or the exchange fails.
        """
        caller = current_caller()
        subject_token = caller.subject_token if caller else None
        if not subject_token:
            raise CredentialUnavailable(
                f"no signed-in user to act for: {audience} accepts only per-user "
                "tokens, which this channel cannot obtain"
            )
        key = (hashlib.sha256(subject_token.encode()).hexdigest(), audience, scope or "")
        now = self._clock()
        if (hit := self._cache.get(key)) and hit.expires_at > now:
            self._cache.move_to_end(key)
            return hit.token
        task = self._inflight.get(key)
        if task is None:
            # Its own task, so a caller cancelled mid-exchange (a call deadline)
            # doesn't cancel it for the others waiting on the same key.
            task = asyncio.create_task(self._fetch(key, subject_token, audience, scope))
            self._inflight[key] = task
        return await asyncio.shield(task)

    async def _fetch(
        self, key: tuple[str, str, str], subject_token: str, audience: str, scope: str | None
    ) -> str:
        try:
            token, lifetime = await asyncio.to_thread(
                self._exchange, subject_token, audience, scope
            )
            self._store(key, token, lifetime)
            return token
        finally:
            del self._inflight[key]

    def _store(self, key: tuple[str, str, str], token: str, lifetime: float) -> None:
        usable = lifetime - EXPIRY_SKEW_SECONDS
        if usable <= 0:
            return  # too short-lived to reuse: used once, not cached
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
                f"the identity provider could not be reached for {audience}", transient=True
            ) from exc

        if response.status_code >= 500 or response.status_code == 429:
            logger.warning("Token exchange for %s: IdP answered %s", audience, response.status_code)
            raise CredentialUnavailable(
                f"the identity provider is unavailable for {audience}", transient=True
            )
        body = _json(response)
        if response.status_code != 200:
            # RFC 6749 §5.2: `error` is a code, safe to log; the description
            # may echo request details, so it is not.
            logger.info(
                "Token exchange for %s refused: %s %s",
                audience,
                response.status_code,
                body.get("error", "unknown_error"),
            )
            raise CredentialUnavailable(f"the identity provider refused a token for {audience}")
        token = body.get("access_token")
        token_type = str(body.get("token_type", "")).lower()
        if not isinstance(token, str) or not token or token_type not in ("bearer", "n_a"):
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
    _broker = None
