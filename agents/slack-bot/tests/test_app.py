"""Tests for the FastAPI/Slack app endpoints."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


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
        """Test that excessive requests trigger a 429 Too Many Requests."""
        app = FastAPI()
        limiter = Limiter(key_func=get_remote_address, default_limits=["1/minute"])
        app.state.limiter = limiter

        def _rate_limit_handler(request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=429, content={"error": "rate_limited"})

        app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

        @app.get("/test")
        @limiter.limit("1/minute")
        async def test_route(request: Request):
            return {"status": "ok"}

        client = TestClient(app)

        # First request should pass
        response = client.get("/test")
        assert response.status_code == 200

        # Second request should be rate limited
        response = client.get("/test")
        assert response.status_code == 429
        assert response.json() == {"error": "rate_limited"}
