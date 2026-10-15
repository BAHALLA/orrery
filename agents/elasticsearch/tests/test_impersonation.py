"""Elasticsearch calls run as the verified caller (es-security-runas-user)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import elasticsearch_agent.tools as tools
from orrery_core.security.identity import CallerIdentity, caller_scope


@pytest.fixture
def session(monkeypatch):
    s = MagicMock()
    s.get.return_value = MagicMock(status_code=200, json=lambda: {"status": "green"})
    monkeypatch.setattr(tools, "_session", s)
    monkeypatch.delenv("ORRERY_IMPERSONATION_FALLBACK", raising=False)
    return s


@pytest.mark.asyncio
async def test_runs_as_the_caller_when_enabled(monkeypatch, session):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "elasticsearch")

    with caller_scope(CallerIdentity(subject="u", username="alice@example.com")):
        await tools._http_get("/_cluster/health")

    assert session.get.call_args.kwargs["headers"] == {
        "es-security-runas-user": "alice@example.com"
    }


@pytest.mark.asyncio
async def test_refuses_without_a_verified_caller(monkeypatch, session):
    monkeypatch.setenv("ORRERY_IMPERSONATION", "elasticsearch")

    resp = await tools._http_get("/_cluster/health")

    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "impersonation_required"
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_off_by_default(monkeypatch, session):
    monkeypatch.delenv("ORRERY_IMPERSONATION", raising=False)

    await tools._http_get("/_cluster/health")

    assert session.get.call_args.kwargs["headers"] is None
