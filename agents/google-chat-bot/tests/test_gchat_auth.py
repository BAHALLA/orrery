"""Tests for Google Chat webhook token verification.

Tokens are real RS256 JWTs signed with a throwaway key and verified by
google-auth itself. Only the certificate *download* is replaced (the
injectable ``fetch``), so these assertions cover the real signature, audience
and expiry checks, plus how often the bot would contact googleapis.com.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt
from google.auth import jwt as google_jwt
from google_chat_bot import auth
from google_chat_bot.auth import (
    CertFetchError,
    GoogleCertCache,
    verify_google_chat_token,
    verify_google_chat_token_async,
)
from google_chat_bot.handler import event_summary

AUDIENCE = "https://bot.example.com/"
CHAT_SA = "chat-system@example.com"
IDENTITIES = frozenset({CHAT_SA, "addon-system@example.com"})


class Key:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        self.public_pem = (
            private.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )

    def sign(self, *, kid: str | None = "same", **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": AUDIENCE,
            "email": CHAT_SA,
            "email_verified": True,
            "sub": "1234",
            "iat": now,
            "exp": now + 300,
            **overrides,
        }
        signer = crypt.RSASigner.from_string(self.private_pem)
        header = {} if kid is None else {"kid": self.kid if kid == "same" else kid}
        token = google_jwt.encode(signer, claims, header=header)
        return token.decode() if isinstance(token, bytes) else token


class FakeGoogle:
    """The certificate endpoint: serves ``{kid: pem}`` and counts downloads."""

    def __init__(self, *keys: Key, cache_control: str | None = "public, max-age=3600") -> None:
        self.certs = {k.kid: k.public_pem for k in keys}
        self.cache_control = cache_control
        self.fetches = 0
        self.fail = False
        self.delay = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> tuple[dict[str, str], str | None]:
        with self._lock:
            self.fetches += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OSError("network unreachable")
        return dict(self.certs), self.cache_control


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def key() -> Key:
    return Key("key-1")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def google(key) -> FakeGoogle:
    return FakeGoogle(key)


@pytest.fixture
def cache(google, clock) -> GoogleCertCache:
    return GoogleCertCache(fetch=google, clock=clock)


def verify(token: str, cache: GoogleCertCache) -> dict | None:
    return verify_google_chat_token(token, AUDIENCE, IDENTITIES, cert_cache=cache)


# ── Claims ──────────────────────────────────────────────────────────


class TestClaims:
    def test_valid_chat_token(self, key, cache):
        payload = verify(key.sign(), cache)

        assert payload is not None
        assert payload["email"] == CHAT_SA

    def test_second_allowed_identity(self, key, cache):
        assert verify(key.sign(email="addon-system@example.com"), cache) is not None

    def test_identity_match_is_case_insensitive(self, key, cache):
        assert verify(key.sign(email="Chat-System@Example.com"), cache) is not None

    def test_other_google_identity_rejected(self, key, cache):
        """A valid Google token for our audience, minted for someone else."""
        assert verify(key.sign(email="attacker@gmail.com"), cache) is None

    def test_token_without_email_rejected(self, key, cache):
        assert verify(key.sign(email=None), cache) is None

    def test_non_google_issuer_rejected(self, key, cache):
        assert verify(key.sign(iss="https://evil.example.com"), cache) is None

    def test_unverified_email_rejected(self, key, cache):
        assert verify(key.sign(email_verified=False), cache) is None

    def test_wrong_audience_rejected(self, key, cache):
        assert verify(key.sign(aud="https://someone-else.example.com/"), cache) is None

    def test_expired_token_rejected(self, key, cache):
        now = int(time.time())
        assert verify(key.sign(iat=now - 7200, exp=now - 3600), cache) is None

    def test_forged_signature_under_a_real_kid_rejected(self, key, cache):
        impostor = Key(key.kid)  # same kid, different private key
        assert verify(impostor.sign(), cache) is None


# ── Header screening ─────────────────────────────────────────────────


class TestHeaderScreening:
    """Malformed tokens are refused before any certificate lookup."""

    def test_missing_kid_never_fetches(self, key, cache, google):
        assert verify(key.sign(kid=None), cache) is None
        assert google.fetches == 0

    def test_oversized_kid_never_fetches(self, key, cache, google):
        assert verify(key.sign(kid="k" * 257), cache) is None
        assert google.fetches == 0

    @pytest.mark.parametrize("token", ["", "garbage", "!!!.???.***", "e30.e30.sig"])
    def test_garbage_never_fetches(self, token, cache, google):
        assert verify(token, cache) is None
        assert google.fetches == 0

    def test_non_rs256_alg_never_fetches(self, cache, google):
        hs = google_jwt.encode(
            crypt.RSASigner.from_string(Key("x").private_pem), {"sub": "x"}, header={"kid": "x"}
        )
        tampered = hs.decode().replace(hs.decode().split(".")[0], "eyJhbGciOiJIUzI1NiJ9", 1)
        assert verify(tampered, cache) is None
        assert google.fetches == 0


# ── Certificate caching ──────────────────────────────────────────────


class TestCertificateCache:
    def test_certificates_are_fetched_once_and_reused(self, key, cache, google):
        """Regression: verify_oauth2_token downloaded the certs on every event."""
        for _ in range(10):
            assert verify(key.sign(), cache) is not None

        assert google.fetches == 1

    def test_cache_control_max_age_is_honoured(self, key, cache, google, clock):
        verify(key.sign(), cache)
        clock.now += 3599
        verify(key.sign(), cache)
        assert google.fetches == 1

        clock.now += 2
        verify(key.sign(), cache)
        assert google.fetches == 2

    def test_missing_max_age_uses_default_ttl(self, key, clock):
        google = FakeGoogle(key, cache_control=None)
        cache = GoogleCertCache(fetch=google, clock=clock)
        verify(key.sign(), cache)
        clock.now += auth.DEFAULT_CERT_TTL_SECONDS + 1
        verify(key.sign(), cache)

        assert google.fetches == 2

    def test_huge_max_age_is_capped(self, key, clock):
        google = FakeGoogle(key, cache_control="max-age=99999999")
        cache = GoogleCertCache(fetch=google, clock=clock)
        verify(key.sign(), cache)
        clock.now += auth.MAX_CERT_TTL_SECONDS + 1
        verify(key.sign(), cache)

        assert google.fetches == 2

    def test_rotation_is_picked_up_with_one_early_refresh(self, key, cache, google):
        verify(key.sign(), cache)
        new_key = Key("key-2")
        google.certs[new_key.kid] = new_key.public_pem  # Google publishes a new key

        assert verify(new_key.sign(), cache) is not None
        assert google.fetches == 2

    def test_forged_kids_cannot_drive_downloads(self, key, cache, google, clock):
        verify(key.sign(), cache)
        for i in range(50):
            assert verify(key.sign(kid=f"forged-{i}"), cache) is None
        assert google.fetches == 2  # one early refresh, then throttled

        clock.now += auth.UNKNOWN_KID_REFRESH_INTERVAL_SECONDS
        verify(key.sign(kid="forged-late"), cache)
        assert google.fetches == 3

    def test_concurrent_cold_start_downloads_once(self, key, cache, google):
        google.delay = 0.05
        token = key.sign()
        results: list[dict | None] = []

        threads = [
            threading.Thread(target=lambda: results.append(verify(token, cache))) for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 20 and all(r is not None for r in results)
        assert google.fetches == 1


class TestGoogleOutage:
    def test_refresh_failure_keeps_serving_the_cached_set(self, key, cache, google, clock):
        verify(key.sign(), cache)
        google.fail = True
        clock.now += 3601  # cache expired, refresh fails

        assert verify(key.sign(), cache) is not None
        # ...and does not retry on every event while Google is down.
        verify(key.sign(), cache)
        assert google.fetches == 2

    def test_no_cached_set_fails_closed_and_backs_off(self, key, cache, google, clock):
        google.fail = True

        assert verify(key.sign(), cache) is None
        assert verify(key.sign(), cache) is None
        assert google.fetches == 1  # the second event did not re-download

        clock.now += auth.FAILED_FETCH_BACKOFF_SECONDS
        google.fail = False
        assert verify(key.sign(), cache) is not None
        assert google.fetches == 2

    def test_empty_certificate_document_fails_closed(self, key, clock):
        google = FakeGoogle(key)
        google.certs = {}
        cache = GoogleCertCache(fetch=google, clock=clock)

        assert verify(key.sign(), cache) is None

    def test_non_200_download_is_a_fetch_error(self):
        response = MagicMock(status=503, data=b"", headers={})
        transport = MagicMock(return_value=response)
        with (
            patch.object(auth.requests, "Request", return_value=transport),
            pytest.raises(CertFetchError, match="503"),
        ):
            auth._fetch_google_certs()

    def test_download_uses_a_short_timeout(self):
        response = MagicMock(status=200, data=b'{"k": "pem"}', headers={"cache-control": "x"})
        transport = MagicMock(return_value=response)
        with patch.object(auth.requests, "Request", return_value=transport):
            certs, cache_control = auth._fetch_google_certs()

        assert certs == {"k": "pem"}
        assert cache_control == "x"
        assert transport.call_args.kwargs["timeout"] == auth.CERT_FETCH_TIMEOUT_SECONDS


# ── Event loop ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slow_certificate_download_does_not_stall_the_event_loop(key, google, clock):
    """Regression: verification ran inline in the async webhook handler."""
    google.delay = 0.3
    cache = GoogleCertCache(fetch=google, clock=clock)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        payload = await verify_google_chat_token_async(
            key.sign(), AUDIENCE, IDENTITIES, cert_cache=cache
        )
    finally:
        beat.cancel()

    assert payload is not None
    assert ticks >= 10


# ── Log hygiene ───────────────────────────────────────────────────────


class TestEventSummary:
    def test_chat_api_event_keeps_resource_names_only(self):
        event = {
            "type": "MESSAGE",
            "space": {"name": "spaces/AAA", "displayName": "SRE war room"},
            "message": {
                "name": "spaces/AAA/messages/1",
                "text": "restart payments, password is hunter2",
                "thread": {"name": "spaces/AAA/threads/T"},
                "sender": {"email": "alice@example.com", "displayName": "Alice"},
            },
            "user": {"email": "alice@example.com"},
        }
        summary = event_summary(event)

        assert summary == {
            "type": "MESSAGE",
            "space": "spaces/AAA",
            "thread": "spaces/AAA/threads/T",
            "message": "spaces/AAA/messages/1",
        }
        rendered = repr(summary)
        for secret in ("hunter2", "alice", "Alice", "war room"):
            assert secret not in rendered

    def test_addons_event_shape(self):
        event = {
            "chat": {
                "messagePayload": {
                    "space": {"name": "spaces/B"},
                    "message": {"name": "spaces/B/messages/2", "text": "secret words"},
                }
            },
            "commonEventObject": {"invokedFunction": "confirm_action"},
        }

        assert event_summary(event) == {
            "type": "MESSAGE",
            "space": "spaces/B",
            "message": "spaces/B/messages/2",
            "invoked_function": "confirm_action",
        }

    @pytest.mark.parametrize("event", [None, [], "text", {"chat": "x", "message": 3}])
    def test_malformed_events_do_not_raise(self, event):
        assert isinstance(event_summary(event), dict)
