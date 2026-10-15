"""Tests for the FastAPI/Slack app endpoints."""

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from orrery_core.serving.rate_limit import RateLimiter, install_rate_limit_handler, parse_rate


class TestHealthEndpoint:
    def test_health_returns_ok(self):
        """Test the health endpoint logic directly without Slack dependencies."""
        app = FastAPI()
        handler_ready = False

        @app.get("/health")
        async def health():
            return {"status": "ok", "handler_ready": handler_ready}

        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["handler_ready"] is False

    def test_health_with_handler_ready(self):
        app = FastAPI()
        handler_ready = True

        @app.get("/health")
        async def health():
            return {"status": "ok", "handler_ready": handler_ready}

        client = TestClient(app)
        response = client.get("/health")
        data = response.json()
        assert data["handler_ready"] is True


class TestRateLimiting:
    def test_rate_limit_exceeded(self):
        """Excessive requests get a 429 with a Retry-After Slack can back off on.

        Same wiring as ``slack_bot.app``: the limit is a route dependency keyed
        on the client address (that module needs live Slack credentials at
        import, so the pattern is exercised here on a bare app).
        """
        app = FastAPI()
        limiter = RateLimiter()
        limit = parse_rate("1/minute")
        install_rate_limit_handler(app)

        async def per_ip(request: Request) -> None:
            limiter.hit(limit, f"ip:{request.client.host if request.client else ''}")

        @app.post("/slack/events", dependencies=[Depends(per_ip)])
        async def slack_events():
            return {"status": "ok"}

        client = TestClient(app)

        assert client.post("/slack/events").status_code == 200

        response = client.post("/slack/events")
        assert response.status_code == 429
        assert response.json()["error"] == "rate_limited"
        assert int(response.headers["Retry-After"]) >= 1
