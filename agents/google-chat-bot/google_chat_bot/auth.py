"""Authentication and verification for Google Chat events.

Every HTTP webhook carries a Google-signed OIDC ID token, and verifying it
needs Google's current signing certificates. ``id_token.verify_oauth2_token``
downloads them **on every call** (google-auth caches only when handed a
caching HTTP session), synchronously, and this bot called it from inside its
async webhook handler — one blocking HTTPS round-trip to googleapis.com per
event, holding up every other request the worker was serving. Anyone who can
reach the endpoint could trigger that, token or no token.

This module keeps google-auth for everything cryptographic (signature, ``aud``,
``exp``/``iat``) and replaces only the certificate source with
:class:`GoogleCertCache`: fetched once, reused for as long as Google's own
``Cache-Control: max-age`` says, refreshed early at most once per minute for a
key id it has not seen (Google publishes a new key before signing with it, so
one refresh covers a rotation, while forged key ids cannot drive fetches), and
bounded by a short timeout. :func:`verify_google_chat_token_async` runs the
whole check off the event loop.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from google.auth import exceptions as google_auth_exceptions
from google.auth import jwt as google_jwt
from google.auth.transport import requests

logger = logging.getLogger("google_chat_bot.auth")

#: Google's OIDC signing certificates, as ``{kid: PEM certificate}``.
GOOGLE_OAUTH2_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"

# Google Chat events for HTTP-endpoint apps are minted via Google's OIDC
# flow and carry ``iss = https://accounts.google.com`` with the Chat system
# service account in the ``email`` claim.
_STANDARD_ISSUERS = frozenset(
    {
        "https://accounts.google.com",
        "accounts.google.com",
    }
)

#: Used when Google's response carries no usable ``max-age``.
DEFAULT_CERT_TTL_SECONDS = 3600.0
#: Upper bound on trusting one certificate download, whatever the header says.
MAX_CERT_TTL_SECONDS = 6 * 3600.0
#: Minimum gap between early refreshes triggered by an unknown key id.
UNKNOWN_KID_REFRESH_INTERVAL_SECONDS = 60.0
#: Bound on one certificate download.
CERT_FETCH_TIMEOUT_SECONDS = 5.0
#: After a failed download with nothing cached, wait this long before trying
#: again, so an outage is not met with one blocked download per webhook.
FAILED_FETCH_BACKOFF_SECONDS = 5.0
#: Tolerated clock drift between Google and this host when checking iat/exp.
CLOCK_SKEW_SECONDS = 10

_MAX_AGE = re.compile(r"max-age=(\d+)")
_MAX_KID_LENGTH = 256


class CertFetchError(Exception):
    """Google's certificates could not be downloaded."""


def _unverified_header(token: str) -> dict[str, Any] | None:
    """Decode a JWT header without verifying anything; ``None`` if malformed."""
    segment = token.split(".", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        header = json.loads(raw)
    except binascii.Error, ValueError, UnicodeDecodeError:
        return None
    return header if isinstance(header, dict) else None


def _ttl_from_cache_control(value: str | None) -> float:
    match = _MAX_AGE.search(value or "")
    if not match:
        return DEFAULT_CERT_TTL_SECONDS
    return min(float(match.group(1)), MAX_CERT_TTL_SECONDS)


class GoogleCertCache:
    """Google's signing certificates, fetched rarely and never on the hot path.

    Thread-safe: verification runs on worker threads, and one lock both
    serializes downloads (concurrent requests after expiry fetch once, not N
    times) and guards the unknown-key-id rate limit.

    Args:
        fetch: Returns ``(certs, cache_control_header)``. Defaults to an HTTPS
            GET of :data:`GOOGLE_OAUTH2_CERTS_URL`; injectable for tests.
        clock: Monotonic clock; injectable for tests.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[], tuple[Mapping[str, str], str | None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        refresh_interval: float = UNKNOWN_KID_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._fetch = fetch or _fetch_google_certs
        self._clock = clock
        self._refresh_interval = refresh_interval
        self._lock = threading.Lock()
        self._certs: Mapping[str, str] = {}
        self._expires_at = -math.inf
        self._last_early_refresh = -math.inf
        self._next_attempt_after_failure = -math.inf

    def certs_for(self, kid: str) -> Mapping[str, str]:
        """Return the current certificate map, refreshed if it cannot serve *kid*.

        The returned map may still lack *kid* (a forged or retired key id);
        google-auth's decode then rejects the token.

        Raises:
            CertFetchError: A download was needed and failed, and there is no
                previously fetched set to fall back on.
        """
        with self._lock:
            now = self._clock()
            expired = now >= self._expires_at
            unknown = kid not in self._certs
            early_refresh_allowed = now - self._last_early_refresh >= self._refresh_interval

            if expired or (unknown and early_refresh_allowed):
                if unknown and not expired:
                    self._last_early_refresh = now
                    logger.info("Google certs: unknown key id %r, refreshing early", kid)
                self._refresh(now)
            return self._certs

    def _refresh(self, now: float) -> None:
        if not self._certs and now < self._next_attempt_after_failure:
            raise CertFetchError("Google certs unavailable (recent download failed)")
        try:
            certs, cache_control = self._fetch()
        except Exception as exc:
            self._next_attempt_after_failure = now + FAILED_FETCH_BACKOFF_SECONDS
            if self._certs:
                # Serve the previous set rather than fail every webhook while
                # googleapis.com is unreachable; retry after one interval.
                logger.warning("Google certs refresh failed, keeping cached set: %s", exc)
                self._expires_at = now + self._refresh_interval
                return
            raise CertFetchError(str(exc)) from exc
        if not isinstance(certs, Mapping) or not certs:
            self._next_attempt_after_failure = now + FAILED_FETCH_BACKOFF_SECONDS
            if self._certs:
                logger.warning("Google certs endpoint returned no certificates, keeping cached set")
                self._expires_at = now + self._refresh_interval
                return
            raise CertFetchError("Google certs endpoint returned no certificates")
        self._certs = dict(certs)
        self._expires_at = now + _ttl_from_cache_control(cache_control)


def _fetch_google_certs() -> tuple[Mapping[str, str], str | None]:
    """Download Google's OIDC certificates (blocking; call off the event loop)."""
    response = requests.Request()(
        url=GOOGLE_OAUTH2_CERTS_URL, method="GET", timeout=CERT_FETCH_TIMEOUT_SECONDS
    )
    if response.status != 200:
        raise CertFetchError(f"HTTP {response.status} from {GOOGLE_OAUTH2_CERTS_URL}")
    return json.loads(response.data), response.headers.get("cache-control")


_cert_cache = GoogleCertCache()


def verify_google_chat_token(
    token: str,
    audience: str,
    valid_identities: frozenset[str],
    *,
    cert_cache: GoogleCertCache | None = None,
) -> dict | None:
    """Verify a Google-signed ID token from a Google Chat webhook event.

    Blocking when the certificate cache needs a refresh; async callers use
    :func:`verify_google_chat_token_async`.

    Args:
        token: The bearer token from the ``Authorization`` header.
        audience: The token audience configured in the Chat API console —
            for HTTP-endpoint apps this is the endpoint URL exactly as
            entered in the Configuration tab (including any trailing slash).
        valid_identities: Set of (lower-cased) service account emails that are
            allowed to sign Chat events.
        cert_cache: Certificate source; defaults to the process-wide cache.

    Returns:
        The decoded payload if the signature, issuer, audience, and service
        account identity all check out. ``None`` on any failure.
    """
    header = _unverified_header(token)
    kid = header.get("kid") if header else None
    if header is None or header.get("alg") != "RS256":
        logger.warning("Token rejected: malformed header or unexpected alg")
        return None
    if not isinstance(kid, str) or not kid or len(kid) > _MAX_KID_LENGTH:
        logger.warning("Token rejected: missing or malformed key id")
        return None

    try:
        certs = (cert_cache or _cert_cache).certs_for(kid)
    except CertFetchError as exc:
        logger.error("Cannot verify Google Chat token, certificates unavailable: %s", exc)
        return None

    try:
        payload = google_jwt.decode(
            token, certs=certs, audience=audience, clock_skew_in_seconds=CLOCK_SKEW_SECONDS
        )
    except (ValueError, google_auth_exceptions.GoogleAuthError) as e:
        logger.warning("Token verification failed: %s", e)
        return None

    issuer = payload.get("iss")
    if issuer not in _STANDARD_ISSUERS:
        logger.warning("Invalid token issuer: %s", issuer)
        return None

    # Prove the token was minted for one of our valid Chat identities and
    # not some other Google identity that happens to have a valid OIDC
    # token for our audience.
    identity = str(payload.get("email") or "").lower()
    if identity not in valid_identities:
        logger.warning("Token is not from Google Chat: identity=%s", identity or None)
        return None

    # If ``email_verified`` is explicitly False, reject; missing is fine.
    if payload.get("email_verified") is False:
        logger.warning("Token email claim is not verified")
        return None

    return payload


async def verify_google_chat_token_async(
    token: str,
    audience: str,
    valid_identities: frozenset[str],
    *,
    cert_cache: GoogleCertCache | None = None,
) -> dict | None:
    """:func:`verify_google_chat_token` without blocking the event loop."""
    return await asyncio.to_thread(
        verify_google_chat_token, token, audience, valid_identities, cert_cache=cert_cache
    )
