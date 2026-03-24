"""Tools demonstrating ADK state and memory patterns.

State prefix reference:
  ctx.state["key"]         → session-scoped (current session only)
  ctx.state["user:key"]    → user-scoped (shared across sessions for same user)
  ctx.state["app:key"]     → app-scoped (shared across all users)
  ctx.state["temp:key"]    → temporary (not persisted at all, current invocation only)
"""

from datetime import UTC, datetime
from typing import Any

from google.adk.tools import ToolContext

from ai_agents_core.validation import validate_string, validate_url

# ── Session State: tracks what happened in this conversation ───────────


def log_operation(ctx: ToolContext, operation: str, details: str) -> dict[str, Any]:
    """Logs an operation to the current session's activity log.

    Use this to track what actions have been performed in this session.

    Args:
        ctx: The tool context (injected by ADK).
        operation: Short name of the operation (e.g., "health_check", "deploy").
        details: Description of what was done.

    Returns:
        Confirmation of the logged operation.
    """
    if err := validate_string(operation, "operation", max_len=100):
        return err
    if err := validate_string(details, "details", max_len=5000):
        return err

    log = ctx.state.get("session_log", [])
    entry = {
        "operation": operation,
        "details": details,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    log.append(entry)
    ctx.state["session_log"] = log

    return {
        "status": "success",
        "message": f"Logged operation '{operation}' to session.",
        "total_operations": len(log),
    }


def get_session_summary(ctx: ToolContext) -> dict[str, Any]:
    """Returns a summary of all operations performed in this session.

    Args:
        ctx: The tool context (injected by ADK).

    Returns:
        A summary of the current session's activity.
    """
    log = ctx.state.get("session_log", [])
    return {
        "status": "success",
        "total_operations": len(log),
        "operations": log,
    }


# ── User State: persists across sessions for the same user ─────────────


def save_note(
    ctx: ToolContext, title: str, content: str, tags: str | None = None
) -> dict[str, Any]:
    """Saves a note that persists across sessions for this user.

    Use this to record findings, incidents, or anything worth remembering.

    Args:
        ctx: The tool context (injected by ADK).
        title: Short title for the note.
        content: The note content.
        tags: Optional comma-separated tags (e.g., "kafka,incident,resolved").

    Returns:
        Confirmation with the note ID.
    """
    if err := validate_string(title, "title", max_len=200):
        return err
    if err := validate_string(content, "content", max_len=10_000):
        return err
    if tags is not None and (err := validate_string(tags, "tags", max_len=500)):
        return err

    notes = ctx.state.get("user:notes", [])
    note_id = len(notes) + 1
    note = {
        "id": note_id,
        "title": title,
        "content": content,
        "tags": [t.strip() for t in tags.split(",")] if tags else [],
        "created_at": datetime.now(UTC).isoformat(),
    }
    notes.append(note)
    ctx.state["user:notes"] = notes

    # Also log this as a session operation
    log = ctx.state.get("session_log", [])
    log.append(
        {
            "operation": "save_note",
            "details": f"Saved note #{note_id}: {title}",
            "timestamp": note["created_at"],
        }
    )
    ctx.state["session_log"] = log

    return {
        "status": "success",
        "message": f"Note #{note_id} saved.",
        "note_id": note_id,
    }


def list_notes(ctx: ToolContext, tag: str | None = None) -> dict[str, Any]:
    """Lists all saved notes for this user, optionally filtered by tag.

    Args:
        ctx: The tool context (injected by ADK).
        tag: Optional tag to filter by.

    Returns:
        A list of saved notes.
    """
    notes = ctx.state.get("user:notes", [])
    if tag:
        notes = [n for n in notes if tag in n.get("tags", [])]

    return {
        "status": "success",
        "count": len(notes),
        "notes": notes,
    }


def search_notes(ctx: ToolContext, query: str) -> dict[str, Any]:
    """Searches saved notes by keyword in title or content.

    Args:
        ctx: The tool context (injected by ADK).
        query: Search term to look for in note titles and content.

    Returns:
        Matching notes.
    """
    if err := validate_string(query, "query", max_len=500):
        return err

    notes = ctx.state.get("user:notes", [])
    query_lower = query.lower()
    matches = [
        n for n in notes if query_lower in n["title"].lower() or query_lower in n["content"].lower()
    ]

    return {
        "status": "success",
        "query": query,
        "count": len(matches),
        "notes": matches,
    }


def delete_note(ctx: ToolContext, note_id: int) -> dict[str, Any]:
    """Deletes a saved note by ID.

    Args:
        ctx: The tool context (injected by ADK).
        note_id: The ID of the note to delete.

    Returns:
        Confirmation of deletion.
    """
    notes = ctx.state.get("user:notes", [])
    updated = [n for n in notes if n["id"] != note_id]

    if len(updated) == len(notes):
        return {"status": "error", "message": f"Note #{note_id} not found."}

    ctx.state["user:notes"] = updated
    return {"status": "success", "message": f"Note #{note_id} deleted."}


# ── User Preferences: user-scoped settings ─────────────────────────────


def set_preference(ctx: ToolContext, key: str, value: str) -> dict[str, Any]:
    """Sets a user preference that persists across sessions.

    Args:
        ctx: The tool context (injected by ADK).
        key: Preference name (e.g., "default_cluster", "alert_threshold").
        value: Preference value.

    Returns:
        Confirmation.
    """
    prefs = ctx.state.get("user:preferences", {})
    prefs[key] = value
    ctx.state["user:preferences"] = prefs

    return {
        "status": "success",
        "message": f"Preference '{key}' set to '{value}'.",
    }


def get_preferences(ctx: ToolContext) -> dict[str, Any]:
    """Gets all user preferences.

    Args:
        ctx: The tool context (injected by ADK).

    Returns:
        All saved preferences.
    """
    prefs = ctx.state.get("user:preferences", {})
    return {"status": "success", "preferences": prefs}


# ── App State: shared across all users ─────────────────────────────────


def add_team_bookmark(ctx: ToolContext, name: str, url: str) -> dict[str, Any]:
    """Adds a shared bookmark visible to all users.

    Args:
        ctx: The tool context (injected by ADK).
        name: Bookmark name.
        url: The URL or resource identifier.

    Returns:
        Confirmation.
    """
    if err := validate_string(name, "name", max_len=200):
        return err
    if err := validate_url(url, "url"):
        return err

    bookmarks = ctx.state.get("app:bookmarks", [])
    bookmarks.append({"name": name, "url": url})
    ctx.state["app:bookmarks"] = bookmarks

    return {
        "status": "success",
        "message": f"Team bookmark '{name}' added.",
    }


def list_team_bookmarks(ctx: ToolContext) -> dict[str, Any]:
    """Lists all shared team bookmarks.

    Args:
        ctx: The tool context (injected by ADK).

    Returns:
        All team bookmarks.
    """
    bookmarks = ctx.state.get("app:bookmarks", [])
    return {"status": "success", "count": len(bookmarks), "bookmarks": bookmarks}
