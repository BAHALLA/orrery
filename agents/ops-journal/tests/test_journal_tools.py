"""Unit tests for ops-journal-agent tools.

Uses fake_ctx fixture from conftest to simulate ADK state.
"""

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


def test_log_operation_creates_entry(fake_ctx):
    ctx = fake_ctx()
    result = log_operation(ctx, "health_check", "Checked Kafka cluster")

    assert result["status"] == "success"
    assert result["total_operations"] == 1
    assert len(ctx.state["session_log"]) == 1
    assert ctx.state["session_log"][0]["operation"] == "health_check"


def test_log_operation_appends_to_existing(fake_ctx):
    ctx = fake_ctx()
    log_operation(ctx, "check_1", "first")
    log_operation(ctx, "check_2", "second")

    assert len(ctx.state["session_log"]) == 2


def test_get_session_summary_empty(fake_ctx):
    ctx = fake_ctx()
    result = get_session_summary(ctx)

    assert result["status"] == "success"
    assert result["total_operations"] == 0
    assert result["operations"] == []


def test_get_session_summary_with_entries(fake_ctx):
    ctx = fake_ctx()
    log_operation(ctx, "deploy", "Deployed v2")
    log_operation(ctx, "rollback", "Rolled back v2")

    result = get_session_summary(ctx)
    assert result["total_operations"] == 2
    assert result["operations"][0]["operation"] == "deploy"


# ── User State: save_note / list_notes / search_notes / delete_note ──


def test_save_note_basic(fake_ctx):
    ctx = fake_ctx()
    result = save_note(ctx, "Incident #42", "Kafka broker-2 went down")

    assert result["status"] == "success"
    assert result["note_id"] == 1
    assert len(ctx.state["user:notes"]) == 1
    assert ctx.state["user:notes"][0]["title"] == "Incident #42"
    assert ctx.state["user:notes"][0]["tags"] == []


def test_save_note_with_tags(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "Note", "content", tags="kafka, incident, resolved")

    note = ctx.state["user:notes"][0]
    assert note["tags"] == ["kafka", "incident", "resolved"]


def test_save_note_also_logs_session(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "Test", "content")

    assert len(ctx.state.get("session_log", [])) == 1
    assert ctx.state["session_log"][0]["operation"] == "save_note"


def test_save_multiple_notes_increments_id(fake_ctx):
    ctx = fake_ctx()
    r1 = save_note(ctx, "First", "a")
    r2 = save_note(ctx, "Second", "b")

    assert r1["note_id"] == 1
    assert r2["note_id"] == 2


def test_list_notes_empty(fake_ctx):
    ctx = fake_ctx()
    result = list_notes(ctx)

    assert result["status"] == "success"
    assert result["count"] == 0


def test_list_notes_returns_all(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "A", "a", tags="kafka")
    save_note(ctx, "B", "b", tags="k8s")

    result = list_notes(ctx)
    assert result["count"] == 2


def test_list_notes_filter_by_tag(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "Kafka issue", "details", tags="kafka,incident")
    save_note(ctx, "K8s issue", "details", tags="k8s,incident")

    result = list_notes(ctx, tag="kafka")
    assert result["count"] == 1
    assert result["notes"][0]["title"] == "Kafka issue"


def test_search_notes_by_title(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "Kafka broker down", "broker-2 crashed")
    save_note(ctx, "Redis timeout", "connection pool exhausted")

    result = search_notes(ctx, "kafka")
    assert result["count"] == 1
    assert result["notes"][0]["title"] == "Kafka broker down"


def test_search_notes_by_content(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "Incident", "OOM kill on broker-2")

    result = search_notes(ctx, "OOM")
    assert result["count"] == 1


def test_search_notes_case_insensitive(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "Alert", "CPU spike on node-1")

    result = search_notes(ctx, "cpu")
    assert result["count"] == 1


def test_search_notes_no_match(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "Note", "content")

    result = search_notes(ctx, "nonexistent")
    assert result["count"] == 0


def test_delete_note_success(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "To delete", "temp")

    result = delete_note(ctx, 1)
    assert result["status"] == "success"
    assert len(ctx.state["user:notes"]) == 0


def test_delete_note_not_found(fake_ctx):
    ctx = fake_ctx()
    result = delete_note(ctx, 999)

    assert result["status"] == "error"
    assert "not found" in result["message"]


def test_delete_note_preserves_others(fake_ctx):
    ctx = fake_ctx()
    save_note(ctx, "Keep", "a")
    save_note(ctx, "Delete", "b")

    delete_note(ctx, 2)
    assert len(ctx.state["user:notes"]) == 1
    assert ctx.state["user:notes"][0]["title"] == "Keep"


# ── User Preferences ─────────────────────────────────────────────────


def test_set_preference(fake_ctx):
    ctx = fake_ctx()
    result = set_preference(ctx, "default_cluster", "prod-us-east")

    assert result["status"] == "success"
    assert ctx.state["user:preferences"]["default_cluster"] == "prod-us-east"


def test_set_preference_overwrites(fake_ctx):
    ctx = fake_ctx()
    set_preference(ctx, "theme", "dark")
    set_preference(ctx, "theme", "light")

    assert ctx.state["user:preferences"]["theme"] == "light"


def test_get_preferences_empty(fake_ctx):
    ctx = fake_ctx()
    result = get_preferences(ctx)

    assert result["status"] == "success"
    assert result["preferences"] == {}


def test_get_preferences_returns_all(fake_ctx):
    ctx = fake_ctx()
    set_preference(ctx, "cluster", "prod")
    set_preference(ctx, "region", "us-east")

    result = get_preferences(ctx)
    assert result["preferences"] == {"cluster": "prod", "region": "us-east"}


# ── App State: team bookmarks ────────────────────────────────────────


def test_add_team_bookmark(fake_ctx):
    ctx = fake_ctx()
    result = add_team_bookmark(ctx, "Grafana", "https://grafana.internal")

    assert result["status"] == "success"
    assert len(ctx.state["app:bookmarks"]) == 1
    assert ctx.state["app:bookmarks"][0]["name"] == "Grafana"


def test_add_multiple_bookmarks(fake_ctx):
    ctx = fake_ctx()
    add_team_bookmark(ctx, "Grafana", "https://grafana.internal")
    add_team_bookmark(ctx, "Kibana", "https://kibana.internal")

    assert len(ctx.state["app:bookmarks"]) == 2


def test_list_team_bookmarks_empty(fake_ctx):
    ctx = fake_ctx()
    result = list_team_bookmarks(ctx)

    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["bookmarks"] == []


def test_list_team_bookmarks_returns_all(fake_ctx):
    ctx = fake_ctx()
    add_team_bookmark(ctx, "Grafana", "https://grafana.internal")
    add_team_bookmark(ctx, "PagerDuty", "https://pagerduty.com")

    result = list_team_bookmarks(ctx)
    assert result["count"] == 2
