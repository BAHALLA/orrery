"""Tests for the Google Chat bot FastAPI endpoint."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from google_chat_bot import app as app_module


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient without running lifespan.

    Using the client without a context manager means the real ``lifespan``
    (which would try to import devops_assistant and build an ADK Runner)
    is skipped. We inject a mock handler directly.
    """
    fake = MagicMock()
    fake.handle_event = AsyncMock(return_value={"text": "ok"})
    monkeypatch.setattr(app_module, "_handler", fake)
    monkeypatch.setattr(app_module.config, "google_chat_audience", "123456789012")
    return TestClient(app_module.api), fake


def test_health(client):
    c, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["handler_ready"] is True


def test_missing_authorization_when_verify_enabled(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(app_module.config, "google_chat_verify_token", True)
    resp = c.post("/", json={"type": "MESSAGE"})
    assert resp.status_code == 401


def test_invalid_token_rejected(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(app_module.config, "google_chat_verify_token", True)
    monkeypatch.setattr(
        app_module, "verify_google_chat_token", lambda token, audience, valid_identities: None
    )
    resp = c.post(
        "/",
        json={"type": "MESSAGE"},
        headers={"Authorization": "Bearer fake"},
    )
    assert resp.status_code == 401


def test_valid_token_dispatches_event(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(app_module.config, "google_chat_verify_token", True)
    monkeypatch.setattr(
        app_module,
        "verify_google_chat_token",
        lambda token, audience, valid_identities: {"email": "user@example.com"},
    )
    event = {"type": "MESSAGE", "message": {"argumentText": "hi"}}
    resp = c.post("/", json=event, headers={"Authorization": "Bearer fake"})
    assert resp.status_code == 200
    # response is from fake.handle_event mock
    assert resp.json() == {"text": "ok"}
    fake.handle_event.assert_awaited_once_with(event)


def test_verify_disabled_skips_auth(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(app_module.config, "google_chat_verify_token", False)
    resp = c.post("/", json={"type": "ADDED_TO_SPACE"})
    assert resp.status_code == 200
    fake.handle_event.assert_awaited_once()


def test_exception_returns_generic_message(client, monkeypatch):
    c, fake = client
    fake.handle_event = AsyncMock(side_effect=RuntimeError("boom — secret 42"))
    monkeypatch.setattr(app_module.config, "google_chat_verify_token", False)
    resp = c.post("/", json={"type": "MESSAGE"})
    assert resp.status_code == 200
    body = resp.json()
    message = body["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
    assert "unexpected error" in message["text"]
    # Secret details from the exception must not leak to the client.
    assert "secret 42" not in message["text"]


def test_handler_not_ready_returns_503(monkeypatch):
    monkeypatch.setattr(app_module, "_handler", None)
    monkeypatch.setattr(app_module.config, "google_chat_verify_token", False)
    c = TestClient(app_module.api)
    resp = c.post("/", json={"type": "MESSAGE"})
    assert resp.status_code == 503
