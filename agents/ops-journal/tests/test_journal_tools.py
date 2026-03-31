"""Unit tests for ops-journal-agent tools.

Uses fake_ctx fixture from conftest to simulate ADK state.
"""

import pytest

from ops_journal_agent.tools import (
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
