"""Tests for SecureMemoryService (core/ai_agents_core/memory.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.adk.events import Event
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.genai import types

from ai_agents_core.memory import SecureMemoryService


def _make_event(text: str, event_id: str = "evt-1", author: str = "user") -> Event:
    """Create a minimal Event with text content."""
    return Event(
        id=event_id,
        author=author,
        content=types.Content(
            role="user",
            parts=[types.Part.from_text(text=text)],
        ),
    )


def _make_session(
    events: list[Event],
    app_name: str = "test_app",
    user_id: str = "test_user",
    session_id: str = "sess-1",
) -> Session:
    """Create a minimal Session with events."""
    session = MagicMock(spec=Session)
    session.app_name = app_name
    session.user_id = user_id
    session.id = session_id
    session.events = events
    return session


def _get_text(mem: MemoryEntry) -> str:
    """Extract the first text part from a MemoryEntry, with type narrowing."""
    assert mem.content is not None
    assert mem.content.parts is not None
    assert len(mem.content.parts) > 0
    text = mem.content.parts[0].text
    assert text is not None
    return text


# ── Redaction tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redacts_password_and_token():
    """Passwords and tokens are replaced with [REDACTED]."""
    svc = SecureMemoryService()
    events = [
        _make_event("Config: password=hunter2 and token=abc123xyz", "e1"),
        _make_event("Normal message without secrets", "e2"),
    ]
    session = _make_session(events)

    await svc.add_session_to_memory(session)

    result = await svc.search_memory(app_name="test_app", user_id="test_user", query="Config")
    assert len(result.memories) >= 1
    redacted_text = _get_text(result.memories[0])
    assert "hunter2" not in redacted_text
    assert "abc123xyz" not in redacted_text
    assert "[REDACTED]" in redacted_text


@pytest.mark.asyncio
async def test_redacts_api_key():
    """API key patterns are redacted."""
    svc = SecureMemoryService()
    events = [_make_event("Using api_key=sk-1234567890abcdef", "e1")]
    session = _make_session(events)

    await svc.add_session_to_memory(session)

    result = await svc.search_memory(app_name="test_app", user_id="test_user", query="Using")
    redacted_text = _get_text(result.memories[0])
    assert "sk-1234567890abcdef" not in redacted_text
    assert "[REDACTED]" in redacted_text


@pytest.mark.asyncio
async def test_redacts_private_key():
    """PEM private key blocks are redacted."""
    svc = SecureMemoryService()
    pem = (
        "Found key:\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWep4PAtGoRBh\n"
        "-----END RSA PRIVATE KEY-----\n"
        "end of message"
    )
    events = [_make_event(pem, "e1")]
    session = _make_session(events)

    await svc.add_session_to_memory(session)

    result = await svc.search_memory(app_name="test_app", user_id="test_user", query="Found")
    redacted_text = _get_text(result.memories[0])
    assert "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn" not in redacted_text
    assert "-----BEGIN RSA PRIVATE KEY-----" not in redacted_text
    assert "[REDACTED]" in redacted_text


@pytest.mark.asyncio
async def test_add_events_to_memory_redacts():
    """add_events_to_memory also applies redaction."""
    svc = SecureMemoryService()
    events = [_make_event("secret=mysecretvalue", "e1")]

    await svc.add_events_to_memory(app_name="test_app", user_id="test_user", events=events)

    result = await svc.search_memory(app_name="test_app", user_id="test_user", query="secret")
    # The original secret value should not appear
    for mem in result.memories:
        text = _get_text(mem)
        assert "mysecretvalue" not in text


# ── Max entries tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_entries_enforced():
    """Events are trimmed to max_entries_per_user, keeping most recent."""
    svc = SecureMemoryService(max_entries_per_user=3)
    events = [_make_event(f"Event {i}", f"e{i}") for i in range(7)]
    session = _make_session(events)

    await svc.add_session_to_memory(session)

    result = await svc.search_memory(app_name="test_app", user_id="test_user", query="Event")
    # Only the 3 most recent events should be stored
    assert len(result.memories) == 3
    texts = [_get_text(m) for m in result.memories]
    assert "Event 4" in texts
    assert "Event 5" in texts
    assert "Event 6" in texts
    # Oldest should be trimmed
    assert "Event 0" not in texts


# ── Search delegation tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_delegates_to_inner():
    """Search returns results from the inner service."""
    svc = SecureMemoryService()
    events = [
        _make_event("Kafka broker down in us-east-1", "e1"),
        _make_event("K8s pod crash loop in payment service", "e2"),
    ]
    session = _make_session(events)
    await svc.add_session_to_memory(session)

    result = await svc.search_memory(app_name="test_app", user_id="test_user", query="Kafka")
    assert len(result.memories) >= 1
    assert any("Kafka" in _get_text(m) for m in result.memories)


@pytest.mark.asyncio
async def test_search_scoped_to_user():
    """Searches are scoped to the requesting user."""
    svc = SecureMemoryService()

    # User A stores an event
    events_a = [_make_event("User A incident data", "e1")]
    session_a = _make_session(events_a, user_id="user_a")
    await svc.add_session_to_memory(session_a)

    # User B should not see User A's data
    result = await svc.search_memory(app_name="test_app", user_id="user_b", query="incident")
    assert len(result.memories) == 0


@pytest.mark.asyncio
async def test_empty_session_skipped():
    """Sessions with no events are silently skipped."""
    svc = SecureMemoryService()
    session = _make_session(events=[])

    await svc.add_session_to_memory(session)

    result = await svc.search_memory(app_name="test_app", user_id="test_user", query="anything")
    assert len(result.memories) == 0


@pytest.mark.asyncio
async def test_events_without_content_preserved():
    """Events with no content parts are passed through without error."""
    svc = SecureMemoryService()
    event_no_content = Event(id="e1", author="system", content=None)
    event_with_content = _make_event("valid event", "e2")
    session = _make_session([event_no_content, event_with_content])

    await svc.add_session_to_memory(session)

    result = await svc.search_memory(app_name="test_app", user_id="test_user", query="valid")
    assert len(result.memories) >= 1
