"""Tests for SecureMemoryService (core/orrery_core/memory.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.adk.events import Event
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.genai import types

from orrery_core.persistence.memory import SecureMemoryService


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


# ── Write-time redaction shares the tool-result pattern set ───────────


class TestRedactionParityWithToolResults:
    """Memory once carried a *shorter* pattern list than the tool-result path:
    key=value pairs and PEM blocks, but no bare provider tokens. A `ghp_` pasted
    into chat was scrubbed on the way out of a tool yet stored verbatim, and any
    later `load_memory` recall handed it back. Both paths now share one list."""

    @pytest.mark.parametrize(
        "secret",
        [
            "password=hunter2",
            "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "AKIAIOSFODNN7EXAMPLE",
            "xoxb-1234567890-abcdefghij",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1",
        ],
    )
    def test_secret_shapes_are_redacted_on_write(self, secret):
        service = SecureMemoryService()
        assert "[REDACTED]" in service._redact_text(f"the value is {secret}")

    def test_ordinary_diagnostic_text_is_untouched(self):
        service = SecureMemoryService()
        text = "deployment api 3/3 ready, 0 restarts since 10:04"
        assert service._redact_text(text) == text

    def test_memory_and_tool_result_paths_agree(self):
        """Parity is structural, not coincidental — one shared source."""
        from orrery_core.persistence.memory import _DEFAULT_PATTERNS
        from orrery_core.security.redaction import SECRET_VALUE_PATTERNS

        assert [p for p, _ in SECRET_VALUE_PATTERNS] == _DEFAULT_PATTERNS


# ── Machine principals (ADR-006) ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["m-0123456789abcdef-x", "a2a-0123456789abcdef"])
async def test_a_machine_principals_session_is_not_remembered(session_id):
    """An agent's turns are model output; stored, they'd return as the user's own."""
    svc = SecureMemoryService()
    await svc.add_session_to_memory(_make_session([_make_event("injected")], session_id=session_id))
    await svc.add_events_to_memory(
        app_name="test_app",
        user_id="test_user",
        events=[_make_event("injected too", "e2")],
        session_id=session_id,
    )

    result = await svc.search_memory(app_name="test_app", user_id="test_user", query="injected")
    assert result.memories == []


@pytest.mark.asyncio
async def test_a_machine_principals_turn_recalls_none_of_its_users_memory():
    """Memory is keyed by the user, whom an agent acting for them shares."""
    from orrery_core.security.confirmation_flow import conversation_scope

    svc = SecureMemoryService()
    await svc.add_session_to_memory(_make_session([_make_event("alice's incident notes")]))

    with conversation_scope("m-0123456789abcdef-x"):
        hidden = await svc.search_memory(app_name="test_app", user_id="test_user", query="notes")
    with conversation_scope("0b6e2d3c-5f7a-4c1e-9d2b-8a7f6e5d4c3b"):
        shown = await svc.search_memory(app_name="test_app", user_id="test_user", query="notes")

    assert hidden.memories == []
    assert len(shown.memories) == 1
