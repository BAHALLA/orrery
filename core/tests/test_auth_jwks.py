"""JWKS key resolution: bounded fetching, rotation, and event-loop safety.

These drive a real ``PyJWKClient`` and stub only its network call
(``fetch_data``), so the assertions are about how many times this service
would actually reach the IdP — the property the resolver exists to bound.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from orrery_core.security import auth
from orrery_core.security.auth import (
    AuthError,
    AuthUnavailableError,
    JWTConfig,
    verify_token,
    verify_token_async,
)

JWKS_URL = "https://idp.example/jwks"
CONFIG = JWTConfig(algorithm="RS256", jwks_url=JWKS_URL)


def _keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


def _token(private: rsa.RSAPrivateKey, kid: str | None, **claims: Any) -> str:
    headers = {"kid": kid} if kid is not None else {}
    body = {"sub": "bob", "roles": ["admin"], "exp": int(time.time()) + 600, **claims}
    return pyjwt.encode(body, private, algorithm="RS256", headers=headers)


class FakeIdP:
    """A real HTTP server on localhost serving a JWKS document; counts fetches.

    A real socket rather than a patched urllib: PyJWT's fetch path has changed
    between releases (``urlopen`` → ``build_opener().open``), and this exercises
    whichever one is installed, caching and error translation included.
    """

    def __init__(self, *keys: dict[str, Any]) -> None:
        self.keys = list(keys)
        self.fetches = 0
        self.delay = 0.0
        self._lock = threading.Lock()
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with idp._lock:
                    idp.fetches += 1
                if idp.delay:
                    time.sleep(idp.delay)
                body = json.dumps({"keys": list(idp.keys)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib name
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/jwks"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def key_a() -> rsa.RSAPrivateKey:
    return _keypair()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def idp(monkeypatch, key_a, clock) -> Iterator[FakeIdP]:
    """A FakeIdP, and CONFIG's JWKS URL resolved against it with a fake clock."""
    fake = FakeIdP(_jwk(key_a, "key-a"))
    resolver = auth._JwksKeyResolver(fake.url, clock=clock)
    monkeypatch.setattr(auth, "_jwks_resolvers", {JWKS_URL: resolver})
    yield fake
    fake.close()


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestVerification:
    def test_round_trip(self, idp, key_a):
        ctx = verify_token(_token(key_a, "key-a"), CONFIG)

        assert (ctx.subject, ctx.role) == ("bob", "admin")
        assert idp.fetches == 1

    def test_fetch_is_bounded_by_a_short_timeout(self, idp):
        resolver = auth._jwks_resolvers[JWKS_URL]

        assert resolver._client.timeout == auth.JWKS_FETCH_TIMEOUT_SECONDS

    def test_key_set_is_cached_across_tokens(self, idp, key_a):
        for _ in range(5):
            verify_token(_token(key_a, "key-a"), CONFIG)

        assert idp.fetches == 1

    def test_token_signed_by_unknown_key_with_known_kid_is_rejected(self, idp):
        forged = _token(_keypair(), "key-a")

        with pytest.raises(AuthError, match="Invalid or expired token"):
            verify_token(forged, CONFIG)


class TestUnknownKidCannotDriveFetches:
    """Regression: every token with an unknown ``kid`` used to trigger a JWKS
    re-fetch — an unauthenticated caller could make the service call its IdP
    once per request (and, before verification moved off the event loop,
    stall every other request while it did)."""

    def test_forged_kids_trigger_at_most_one_refresh_per_interval(self, idp, key_a):
        verify_token(_token(key_a, "key-a"), CONFIG)  # warm the cache
        assert idp.fetches == 1

        for i in range(50):
            with pytest.raises(AuthError, match="Invalid or expired token"):
                verify_token(_token(_keypair() if i == 0 else key_a, f"forged-{i}"), CONFIG)

        assert idp.fetches == 2  # one refresh for the first unknown kid, none after

    def test_refresh_allowed_again_after_the_interval(self, idp, key_a, clock):
        verify_token(_token(key_a, "key-a"), CONFIG)
        with pytest.raises(AuthError):
            verify_token(_token(key_a, "forged-1"), CONFIG)
        clock.now += auth.JWKS_UNKNOWN_KID_REFRESH_INTERVAL_SECONDS

        with pytest.raises(AuthError):
            verify_token(_token(key_a, "forged-2"), CONFIG)

        assert idp.fetches == 3

    def test_key_rotation_is_picked_up_by_one_refresh(self, idp, key_a):
        verify_token(_token(key_a, "key-a"), CONFIG)
        key_b = _keypair()
        idp.keys.append(_jwk(key_b, "key-b"))  # the IdP rotates a new key in

        ctx = verify_token(_token(key_b, "key-b"), CONFIG)

        assert ctx.subject == "bob"
        assert idp.fetches == 2

    @pytest.mark.parametrize("kid", [None, "", "k" * 257])
    def test_token_without_a_usable_kid_never_reaches_the_idp(self, idp, key_a, kid):
        with pytest.raises(AuthError):
            verify_token(_token(key_a, kid), CONFIG)

        assert idp.fetches == 0

    def test_non_string_kid_never_reaches_the_idp(self, idp):
        # PyJWT refuses to *encode* this, so build it by hand as an attacker would.
        def b64(obj: dict[str, Any]) -> str:
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

        token = ".".join(
            [b64({"alg": "RS256", "typ": "JWT", "kid": 123}), b64({"sub": "bob"}), "c2ln"]
        )
        with pytest.raises(AuthError):
            verify_token(token, CONFIG)

        assert idp.fetches == 0

    def test_algorithm_mismatch_is_rejected_before_any_key_lookup(self, idp):
        """An HS256 token against an RS256 deployment (the classic algorithm-
        confusion probe) is refused from its header alone."""
        token = pyjwt.encode(
            {"sub": "bob", "exp": int(time.time()) + 60},
            "x" * 64,
            algorithm="HS256",
            headers={"kid": "key-a"},
        )
        with pytest.raises(AuthError):
            verify_token(token, CONFIG)

        assert idp.fetches == 0

    def test_concurrent_cold_start_fetches_once(self, idp, key_a):
        idp.delay = 0.05
        token = _token(key_a, "key-a")
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                verify_token(token, CONFIG)
            except BaseException as exc:  # pragma: no cover — surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert idp.fetches == 1


class TestIdPOutage:
    def test_unreachable_idp_is_unavailable_not_a_bad_token(self, monkeypatch, key_a):
        """An outage isn't the caller's fault and a new token won't fix it."""
        nothing_listening = f"http://127.0.0.1:{_unused_port()}/jwks"
        resolver = auth._JwksKeyResolver(nothing_listening)
        monkeypatch.setattr(auth, "_jwks_resolvers", {JWKS_URL: resolver})

        with pytest.raises(AuthUnavailableError):
            verify_token(_token(key_a, "key-a"), CONFIG)

    def test_unavailable_is_not_an_auth_error(self):
        # The HTTP layer maps AuthError → 401 and this → 503; a subclass
        # relationship would silently turn outages back into "bad token".
        assert not issubclass(AuthUnavailableError, AuthError)

    def test_keyless_jwks_document_is_an_auth_error(self, idp, key_a):
        idp.keys = []

        with pytest.raises(AuthError):
            verify_token(_token(key_a, "key-a"), CONFIG)


class TestEventLoopSafety:
    @pytest.mark.asyncio
    async def test_slow_idp_does_not_stall_the_event_loop(self, idp, key_a):
        """Regression: verification ran inline in the async auth dependency, so
        a JWKS round-trip froze every request the process was serving."""
        idp.delay = 0.3
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            ctx = await verify_token_async(_token(key_a, "key-a"), CONFIG)
        finally:
            beat.cancel()

        assert ctx.subject == "bob"
        assert ticks >= 10  # the loop kept running while the fetch was in flight

    @pytest.mark.asyncio
    async def test_hmac_path_verifies_inline(self, monkeypatch):
        called = False

        async def fail_to_thread(*_a: Any, **_k: Any) -> Any:
            nonlocal called
            called = True
            raise AssertionError("symmetric verification should not use a thread")

        monkeypatch.setattr(auth.asyncio, "to_thread", fail_to_thread)
        secret = "x" * 64
        token = pyjwt.encode(
            {"sub": "alice", "exp": int(time.time()) + 60}, secret, algorithm="HS256"
        )

        ctx = await verify_token_async(token, JWTConfig(algorithm="HS256", secret=secret))

        assert ctx.subject == "alice"
        assert called is False


def test_resolver_is_created_once_per_url(monkeypatch):
    monkeypatch.setattr(auth, "_jwks_resolvers", {})

    first = auth._get_jwks_resolver(JWKS_URL)
    second = auth._get_jwks_resolver(JWKS_URL)

    assert first is second
