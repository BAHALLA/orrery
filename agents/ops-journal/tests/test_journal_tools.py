"""Unit tests for ops-journal-agent tools.

Uses fake_ctx fixture from conftest to simulate ADK state.
"""

import pytest

from ops_journal_agent.tools import (
    MAX_NOTES_PER_USER,
    MAX_PREFERENCES,
    MAX_TEAM_BOOKMARKS,
    add_team_bookmark,
    delete_note,
    get_preferences,
    get_session_summary,
    list_notes,
    list_team_bookmarks,
    log_operation,
    save_note,
    search_notes,
    set_preference,
)
from orrery_core.observability.activity import MAX_SESSION_LOG_ENTRIES
from orrery_core.security.guardrails import get_guard_level

# ── Session State: log_operation / get_session_summary ────────────────


@pytest.mark.asyncio
async def test_log_operation_creates_entry(fake_ctx):
    ctx = fake_ctx()
    result = await log_operation(ctx, "health_check", "Checked Kafka cluster")

    assert result["status"] == "success"
    assert result["total_operations"] == 1
    assert len(ctx.state["session_log"]) == 1
    assert ctx.state["session_log"][0]["operation"] == "health_check"


@pytest.mark.asyncio
async def test_log_operation_appends_to_existing(fake_ctx):
    ctx = fake_ctx()
    await log_operation(ctx, "check_1", "first")
    await log_operation(ctx, "check_2", "second")

    assert len(ctx.state["session_log"]) == 2


@pytest.mark.asyncio
async def test_get_session_summary_empty(fake_ctx):
    ctx = fake_ctx()
    result = await get_session_summary(ctx)

    assert result["status"] == "success"
    assert result["total_operations"] == 0
    assert result["operations"] == []


@pytest.mark.asyncio
async def test_get_session_summary_with_entries(fake_ctx):
    ctx = fake_ctx()
    await log_operation(ctx, "deploy", "Deployed v2")
    await log_operation(ctx, "rollback", "Rolled back v2")

    result = await get_session_summary(ctx)
    assert result["total_operations"] == 2
    assert result["operations"][0]["operation"] == "deploy"


# ── User State: save_note / list_notes / search_notes / delete_note ──


@pytest.mark.asyncio
async def test_save_note_basic(fake_ctx):
    ctx = fake_ctx()
    result = await save_note(ctx, "Incident #42", "Kafka broker-2 went down")

    assert result["status"] == "success"
    assert result["note_id"] == 1
    assert len(ctx.state["user:notes"]) == 1
    assert ctx.state["user:notes"][0]["title"] == "Incident #42"
    assert ctx.state["user:notes"][0]["tags"] == []


@pytest.mark.asyncio
async def test_save_note_with_tags(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "Note", "content", tags="kafka, incident, resolved")

    note = ctx.state["user:notes"][0]
    assert note["tags"] == ["kafka", "incident", "resolved"]


@pytest.mark.asyncio
async def test_save_note_also_logs_session(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "Test", "content")

    assert len(ctx.state.get("session_log", [])) == 1
    assert ctx.state["session_log"][0]["operation"] == "save_note"


@pytest.mark.asyncio
async def test_save_multiple_notes_increments_id(fake_ctx):
    ctx = fake_ctx()
    r1 = await save_note(ctx, "First", "a")
    r2 = await save_note(ctx, "Second", "b")

    assert r1["note_id"] == 1
    assert r2["note_id"] == 2


@pytest.mark.asyncio
async def test_list_notes_empty(fake_ctx):
    ctx = fake_ctx()
    result = await list_notes(ctx)

    assert result["status"] == "success"
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_list_notes_returns_all(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "A", "a", tags="kafka")
    await save_note(ctx, "B", "b", tags="k8s")

    result = await list_notes(ctx)
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_list_notes_filter_by_tag(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "Kafka issue", "details", tags="kafka,incident")
    await save_note(ctx, "K8s issue", "details", tags="k8s,incident")

    result = await list_notes(ctx, tag="kafka")
    assert result["count"] == 1
    assert result["notes"][0]["title"] == "Kafka issue"


@pytest.mark.asyncio
async def test_search_notes_by_title(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "Kafka broker down", "broker-2 crashed")
    await save_note(ctx, "Redis timeout", "connection pool exhausted")

    result = await search_notes(ctx, "kafka")
    assert result["count"] == 1
    assert result["notes"][0]["title"] == "Kafka broker down"


@pytest.mark.asyncio
async def test_search_notes_by_content(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "Incident", "OOM kill on broker-2")

    result = await search_notes(ctx, "OOM")
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_search_notes_case_insensitive(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "Alert", "CPU spike on node-1")

    result = await search_notes(ctx, "cpu")
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_search_notes_no_match(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "Note", "content")

    result = await search_notes(ctx, "nonexistent")
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_delete_note_success(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "To delete", "temp")

    result = await delete_note(ctx, 1)
    assert result["status"] == "success"
    assert len(ctx.state["user:notes"]) == 0


@pytest.mark.asyncio
async def test_delete_note_not_found(fake_ctx):
    ctx = fake_ctx()
    result = await delete_note(ctx, 999)

    assert result["status"] == "error"
    assert "not found" in result["message"]


@pytest.mark.asyncio
async def test_delete_note_preserves_others(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "Keep", "a")
    await save_note(ctx, "Delete", "b")

    await delete_note(ctx, 2)
    assert len(ctx.state["user:notes"]) == 1
    assert ctx.state["user:notes"][0]["title"] == "Keep"


# ── User Preferences ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_preference(fake_ctx):
    ctx = fake_ctx()
    result = await set_preference(ctx, "default_cluster", "prod-us-east")

    assert result["status"] == "success"
    assert ctx.state["user:preferences"]["default_cluster"] == "prod-us-east"


@pytest.mark.asyncio
async def test_set_preference_overwrites(fake_ctx):
    ctx = fake_ctx()
    await set_preference(ctx, "theme", "dark")
    await set_preference(ctx, "theme", "light")

    assert ctx.state["user:preferences"]["theme"] == "light"


@pytest.mark.asyncio
async def test_get_preferences_empty(fake_ctx):
    ctx = fake_ctx()
    result = await get_preferences(ctx)

    assert result["status"] == "success"
    assert result["preferences"] == {}


@pytest.mark.asyncio
async def test_get_preferences_returns_all(fake_ctx):
    ctx = fake_ctx()
    await set_preference(ctx, "cluster", "prod")
    await set_preference(ctx, "region", "us-east")

    result = await get_preferences(ctx)
    assert result["preferences"] == {"cluster": "prod", "region": "us-east"}


# ── App State: team bookmarks ────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_team_bookmark(fake_ctx):
    ctx = fake_ctx()
    result = await add_team_bookmark(ctx, "Grafana", "https://grafana.internal")

    assert result["status"] == "success"
    assert len(ctx.state["app:bookmarks"]) == 1
    assert ctx.state["app:bookmarks"][0]["name"] == "Grafana"


@pytest.mark.asyncio
async def test_add_multiple_bookmarks(fake_ctx):
    ctx = fake_ctx()
    await add_team_bookmark(ctx, "Grafana", "https://grafana.internal")
    await add_team_bookmark(ctx, "Kibana", "https://kibana.internal")

    assert len(ctx.state["app:bookmarks"]) == 2


@pytest.mark.asyncio
async def test_list_team_bookmarks_empty(fake_ctx):
    ctx = fake_ctx()
    result = await list_team_bookmarks(ctx)

    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["bookmarks"] == []


@pytest.mark.asyncio
async def test_list_team_bookmarks_returns_all(fake_ctx):
    ctx = fake_ctx()
    await add_team_bookmark(ctx, "Grafana", "https://grafana.internal")
    await add_team_bookmark(ctx, "PagerDuty", "https://pagerduty.com")

    result = await list_team_bookmarks(ctx)
    assert result["count"] == 2


# ── Input validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_bookmark_rejects_javascript_url(fake_ctx):
    ctx = fake_ctx()
    result = await add_team_bookmark(ctx, "XSS", "javascript:alert(1)")
    assert result["status"] == "error"
    assert "url" in result["message"]


@pytest.mark.asyncio
async def test_add_bookmark_rejects_data_url(fake_ctx):
    ctx = fake_ctx()
    result = await add_team_bookmark(ctx, "Data", "data:text/html,<h1>hi</h1>")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_save_note_rejects_overlong_content(fake_ctx):
    ctx = fake_ctx()
    result = await save_note(ctx, "title", "x" * 10_001)
    assert result["status"] == "error"
    assert "content" in result["message"]


@pytest.mark.asyncio
async def test_save_note_rejects_empty_title(fake_ctx):
    ctx = fake_ctx()
    result = await save_note(ctx, "", "content")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_log_operation_rejects_empty_operation(fake_ctx):
    ctx = fake_ctx()
    result = await log_operation(ctx, "", "details")
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_search_notes_rejects_empty_query(fake_ctx):
    ctx = fake_ctx()
    result = await search_notes(ctx, "")
    assert result["status"] == "error"


# ── State integrity: ids, bounds, validation ──────────────────────────


@pytest.mark.asyncio
async def test_note_ids_are_never_reused_after_delete(fake_ctx):
    """Regression: ids were len(notes)+1, so a delete made the next save reuse
    an id and a later delete_note removed *both* notes carrying it."""
    ctx = fake_ctx()
    await save_note(ctx, "first", "a")
    await save_note(ctx, "second", "b")
    await delete_note(ctx, 1)
    third = await save_note(ctx, "third", "c")

    assert third["note_id"] == 3
    assert [n["id"] for n in ctx.state["user:notes"]] == [2, 3]

    await delete_note(ctx, 2)
    assert [n["title"] for n in ctx.state["user:notes"]] == ["third"]


@pytest.mark.asyncio
async def test_note_ids_not_reused_even_when_newest_is_deleted(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "first", "a")
    await save_note(ctx, "second", "b")
    await delete_note(ctx, 2)
    again = await save_note(ctx, "again", "c")

    assert again["note_id"] == 3


@pytest.mark.asyncio
async def test_delete_note_removes_one_note_from_legacy_duplicate_ids(fake_ctx):
    """Notes saved by the old id scheme can already share an id; deleting it
    must remove exactly one of them, never both."""
    ctx = fake_ctx(
        {
            "user:notes": [
                {"id": 2, "title": "older", "content": "x", "tags": []},
                {"id": 2, "title": "newer", "content": "y", "tags": []},
            ]
        }
    )
    result = await delete_note(ctx, 2)

    assert result["status"] == "success"
    assert [n["title"] for n in ctx.state["user:notes"]] == ["newer"]


@pytest.mark.asyncio
async def test_new_note_id_skips_past_legacy_ids(fake_ctx):
    ctx = fake_ctx({"user:notes": [{"id": 7, "title": "old", "content": "x", "tags": []}]})
    result = await save_note(ctx, "new", "y")

    assert result["note_id"] == 8


@pytest.mark.asyncio
async def test_delete_note_rejects_non_integer_id(fake_ctx):
    ctx = fake_ctx()
    result = await delete_note(ctx, "1")  # ty: ignore[invalid-argument-type]

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_save_note_refused_at_capacity(fake_ctx):
    notes = [
        {"id": i, "title": "t", "content": "c", "tags": []}
        for i in range(1, MAX_NOTES_PER_USER + 1)
    ]
    ctx = fake_ctx({"user:notes": notes})
    result = await save_note(ctx, "one more", "c")

    assert result["status"] == "error"
    assert "limit" in result["message"]
    assert len(ctx.state["user:notes"]) == MAX_NOTES_PER_USER


@pytest.mark.asyncio
async def test_save_note_rejects_malformed_tag(fake_ctx):
    ctx = fake_ctx()
    result = await save_note(ctx, "t", "c", tags="ok, <script>")

    assert result["status"] == "error"
    assert "user:notes" not in ctx.state


@pytest.mark.asyncio
async def test_save_note_dedupes_tags(fake_ctx):
    ctx = fake_ctx()
    await save_note(ctx, "t", "c", tags="kafka, kafka,,incident")

    assert ctx.state["user:notes"][0]["tags"] == ["kafka", "incident"]


@pytest.mark.asyncio
async def test_session_log_is_bounded(fake_ctx):
    """log_operation shares session_log with ActivityPlugin and honours its cap."""
    ctx = fake_ctx()
    for i in range(MAX_SESSION_LOG_ENTRIES + 5):
        result = await log_operation(ctx, "op", f"entry {i}")

    log = ctx.state["session_log"]
    assert len(log) == MAX_SESSION_LOG_ENTRIES
    assert result["total_operations"] == MAX_SESSION_LOG_ENTRIES
    assert log[-1]["details"] == f"entry {MAX_SESSION_LOG_ENTRIES + 4}"


@pytest.mark.asyncio
async def test_save_note_session_log_is_bounded(fake_ctx):
    ctx = fake_ctx({"session_log": [{"operation": "x"}] * MAX_SESSION_LOG_ENTRIES})
    await save_note(ctx, "t", "c")

    assert len(ctx.state["session_log"]) == MAX_SESSION_LOG_ENTRIES
    assert ctx.state["session_log"][-1]["operation"] == "save_note"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("", "v"),
        ("has space", "v"),
        ("ignore previous instructions", "v"),
        ("k" * 65, "v"),
        ("ok_key", ""),
        ("ok_key", "v" * 1001),
        ("ok_key", 42),
    ],
)
async def test_set_preference_validates_input(fake_ctx, key, value):
    ctx = fake_ctx()
    result = await set_preference(ctx, key, value)

    assert result["status"] == "error"
    assert "user:preferences" not in ctx.state


@pytest.mark.asyncio
async def test_set_preference_refuses_new_key_at_capacity_but_allows_overwrite(fake_ctx):
    prefs = {f"key{i}": "v" for i in range(MAX_PREFERENCES)}
    ctx = fake_ctx({"user:preferences": prefs})

    refused = await set_preference(ctx, "brand_new", "v")
    overwritten = await set_preference(ctx, "key0", "changed")

    assert refused["status"] == "error"
    assert overwritten["status"] == "success"
    assert ctx.state["user:preferences"]["key0"] == "changed"
    assert "brand_new" not in ctx.state["user:preferences"]


def test_add_team_bookmark_is_confirmation_gated():
    """App-scoped state reaches every user, so writing it is an operator action."""
    assert get_guard_level(add_team_bookmark) == "confirm"


@pytest.mark.asyncio
async def test_add_team_bookmark_updates_existing_name(fake_ctx):
    ctx = fake_ctx()
    await add_team_bookmark(ctx, "Grafana", "https://grafana.example.com")
    result = await add_team_bookmark(ctx, "grafana", "https://grafana.example.com/new")

    assert "updated" in result["message"]
    assert ctx.state["app:bookmarks"] == [
        {"name": "grafana", "url": "https://grafana.example.com/new"}
    ]


@pytest.mark.asyncio
async def test_add_team_bookmark_refused_at_capacity(fake_ctx):
    bookmarks = [{"name": f"b{i}", "url": "https://x.example"} for i in range(MAX_TEAM_BOOKMARKS)]
    ctx = fake_ctx({"app:bookmarks": bookmarks})
    result = await add_team_bookmark(ctx, "one more", "https://y.example")

    assert result["status"] == "error"
    assert len(ctx.state["app:bookmarks"]) == MAX_TEAM_BOOKMARKS


@pytest.mark.asyncio
async def test_add_team_bookmark_rejects_overlong_url(fake_ctx):
    ctx = fake_ctx()
    result = await add_team_bookmark(ctx, "long", "https://x.example/" + "a" * 2100)

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_list_notes_rejects_malformed_tag(fake_ctx):
    ctx = fake_ctx()
    result = await list_notes(ctx, tag="a b")

    assert result["status"] == "error"
