"""HTTP front-door hardening: security headers, real readiness, rate-limit storage."""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from orrery_core.security.auth import JWTConfig  # noqa: E402
from orrery_core.serving import readiness as readiness_module  # noqa: E402
from orrery_core.serving import server as server_module  # noqa: E402
from orrery_core.serving.readiness import ReadinessCheck  # noqa: E402
from orrery_core.serving.security_headers import build_csp, trusted_origins  # noqa: E402
from orrery_core.serving.server import ServerConfig, create_app  # noqa: E402


@pytest.fixture
def patched_runner():
    session_service = MagicMock()
    session_service.create_session = AsyncMock()
    session_service.get_session = AsyncMock(return_value=None)
    with (
        patch("orrery_core.serving.gateway.Runner", return_value=MagicMock()),
        patch("orrery_core.serving.gateway.App", return_value=MagicMock()),
        patch("orrery_core.serving.server.create_session_service", return_value=session_service),
    ):
        yield session_service


def _app(**config) -> TestClient:
    config.setdefault("auth_enabled", False)
    return TestClient(
        create_app(
            root_agent=MagicMock(name="root"),
            app_name="test",
            plugins=[],
            config=ServerConfig(**config),
        )
    )


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ── Security headers ─────────────────────────────────────────────────


def _csp_directives(header: str) -> dict[str, str]:
    return {part.split(" ", 1)[0]: part for part in (p.strip() for p in header.split(";"))}


def test_api_responses_carry_security_headers_and_are_not_cached(patched_runner):
    r = _app().get("/healthz")

    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["cache-control"] == "no-store"  # transcripts and identity are JSON
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_error_responses_carry_them_too(patched_runner):
    r = _app(auth_enabled=True, jwt=JWTConfig(secret="x" * 64)).get("/me")

    assert r.status_code == 401
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in r.headers


def test_console_is_served_under_a_strict_csp(patched_runner, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>")
    monkeypatch.setattr(server_module, "_static_dir", lambda: tmp_path)

    r = _app(web_console_enabled=True).get("/")

    assert r.status_code == 200
    csp = _csp_directives(r.headers["content-security-policy"])
    assert csp["script-src"] == "script-src 'self'"  # no inline script, no eval, no CDN
    assert csp["object-src"] == "object-src 'none'"
    assert csp["frame-ancestors"] == "frame-ancestors 'none'"
    assert csp["base-uri"] == "base-uri 'none'"
    assert "cache-control" not in r.headers  # static assets stay cacheable


def test_sso_issuer_and_extra_origins_are_allowed_to_connect(patched_runner):
    r = _app(
        jwt=JWTConfig(secret="x" * 64, issuer="https://sso.example.com/realms/orrery"),
        csp_extra_origins=("https://api.example.com", "not a url"),
    ).get("/healthz")

    csp = _csp_directives(r.headers["content-security-policy"])
    assert csp["connect-src"] == (
        "connect-src 'self' https://sso.example.com https://api.example.com"
    )
    assert csp["frame-src"].startswith("frame-src 'self' https://sso.example.com")


def test_docs_are_exempt_from_the_csp_but_not_the_other_headers(patched_runner):
    """FastAPI's Swagger UI loads from a CDN; it is only served when enabled."""
    r = _app(docs_enabled=True).get("/docs")

    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"


def test_trusted_origins_normalises_and_dedupes():
    assert trusted_origins(
        "https://idp.example.com/realms/a/", ["https://idp.example.com/other", "ftp://x", ""]
    ) == ("https://idp.example.com",)


def test_build_csp_without_origins_is_self_only():
    assert "connect-src 'self';" in build_csp()


# ── Readiness ─────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_in_memory_sessions_are_always_ready():
    result = await ReadinessCheck(None).check()

    assert result.ready is True
    assert result.checks == {"database": "not_configured"}


@pytest.mark.asyncio
async def test_result_is_cached_then_rechecked():
    calls: list[str] = []
    answers = iter([True, False])
    clock = Clock()
    check = ReadinessCheck(
        "postgresql://db/x",
        probe=lambda url, connect_timeout: calls.append(url) or next(answers),
        clock=clock,
    )

    assert (await check.check()).ready is True
    assert (await check.check()).ready is True
    assert len(calls) == 1
    clock.now += readiness_module.CACHE_SECONDS
    assert (await check.check()).ready is False
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_concurrent_probes_share_one_check():
    calls = 0

    def slow_probe(url: str, connect_timeout: int) -> bool:
        nonlocal calls
        calls += 1
        import time

        time.sleep(0.05)
        return True

    check = ReadinessCheck("postgresql://db/x", probe=slow_probe)
    results = await asyncio.gather(*(check.check() for _ in range(20)))

    assert all(r.ready for r in results)
    assert calls == 1


@pytest.mark.asyncio
async def test_a_raising_probe_means_not_ready():
    def broken(url: str, connect_timeout: int) -> bool:
        raise RuntimeError("driver exploded")

    result = await ReadinessCheck("postgresql://db/x", probe=broken).check()

    assert result.ready is False
    assert result.checks == {"database": "unreachable"}


def test_readyz_is_503_when_the_session_database_is_down(patched_runner):
    """End to end: a real connection attempt to a closed port, not a mock."""
    port = _closed_port()
    r = _app(database_url=f"postgresql://orrery:secret-pw@127.0.0.1:{port}/orrery").get("/readyz")

    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "checks": {"database": "unreachable"}}
    assert "secret-pw" not in r.text


def test_readyz_is_200_without_a_database(patched_runner):
    r = _app().get("/readyz")

    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_healthz_never_checks_the_database(patched_runner):
    """Liveness must not restart pods over a database outage."""
    port = _closed_port()
    r = _app(database_url=f"postgresql://u:p@127.0.0.1:{port}/x").get("/healthz")

    assert r.status_code == 200


# ── Rate-limit storage ───────────────────────────────────────────────


def test_rate_limit_storage_is_configurable(monkeypatch):
    monkeypatch.setenv("ORRERY_RATE_LIMIT_STORAGE_URI", "redis://redis:6379")

    assert ServerConfig.from_env().rate_limit_storage_uri == "redis://redis:6379"


def test_rate_limit_storage_defaults_to_memory(monkeypatch):
    monkeypatch.delenv("ORRERY_RATE_LIMIT_STORAGE_URI", raising=False)

    assert ServerConfig.from_env().rate_limit_storage_uri == "memory://"


def test_bad_rate_limit_storage_fails_at_startup(patched_runner):
    with pytest.raises(Exception, match="unknown storage scheme"):
        _app(rate_limit_storage_uri="bogus://nowhere")


def test_per_replica_rate_limits_are_called_out(patched_runner, monkeypatch, caplog):
    monkeypatch.setenv("ORRERY_MULTI_REPLICA", "true")
    monkeypatch.setenv("ORRERY_CONFIRMATION_BACKEND", "postgres")

    _app(database_url="postgresql://db/x")

    assert "counted per replica" in caplog.text
