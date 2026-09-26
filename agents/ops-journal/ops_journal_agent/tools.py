"""Tools demonstrating ADK state and memory patterns.

State prefix reference:
  ctx.state["key"]         → session-scoped (current session only)
  ctx.state["user:key"]    → user-scoped (shared across sessions for same user)
  ctx.state["app:key"]     → app-scoped (shared across all users)
  ctx.state["temp:key"]    → temporary (not persisted at all, current invocation only)

Every collection kept here is **bounded**. ADK copies an assigned value whole
into the event's state delta, so an unbounded list makes each write carry the
entire history — the same quadratic growth ``ActivityPlugin`` is capped
against. The session log is trimmed to its most recent entries (a log's value
is its tail); notes, preferences and bookmarks are records the user chose to
keep, so they are never silently dropped — a write past the cap is refused
with a message saying what to remove.
"""

import re
from datetime import UTC, datetime
from typing import Any

from google.adk.tools import ToolContext

from orrery_core import confirm
from orrery_core.observability.activity import MAX_SESSION_LOG_ENTRIES
from orrery_core.security.validation import validate_positive_int, validate_string, validate_url

#: Notes one user may keep. Each is up to ~10 KB, and ``user:`` state rides
#: every session of that user, so this bounds it at a few MB.
MAX_NOTES_PER_USER = 500
#: Preferences one user may keep.
MAX_PREFERENCES = 50
#: Bookmarks the whole team may keep (``app:`` state rides every session).
MAX_TEAM_BOOKMARKS = 100

#: Preference names: short identifiers, so a preference can never be used to
#: smuggle free text (or a prompt) into a key the model later reads back.
PREFERENCE_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
#: Tags: the same shape, so ``list_notes(tag=...)`` compares like with like.
TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,49}$")

#: Highest note id ever issued to this user. Ids are never reused: the model
#: and the user refer to notes as "#3", so a recycled id would silently
#: re-point that reference at a different note.
_NOTES_LAST_ID_KEY = "user:notes_last_id"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _append_to_session_log(ctx: ToolContext, operation: str, details: str) -> list[dict]:
    """Append one entry to ``session_log``, keeping only the most recent ones.

    Same bound and entry shape as :func:`orrery_core.observability.activity.activity_tracker`,
    which writes to the same key.
    """
    log = [
        *ctx.state.get("session_log", []),
        {
            "operation": operation,
            "details": details,
            "timestamp": _now(),
        },
    ]
    if len(log) > MAX_SESSION_LOG_ENTRIES:
        log = log[-MAX_SESSION_LOG_ENTRIES:]
    ctx.state["session_log"] = log
    return log


def _parse_tags(tags: str | None) -> list[str] | dict[str, Any]:
    """Split a comma-separated tag string, validating each tag."""
    if not tags:
        return []
    parsed: list[str] = []
    for raw in tags.split(","):
        tag = raw.strip()
        if not tag:
            continue
        if err := validate_string(tag, "tags", max_len=50, pattern=TAG_PATTERN):
            return err
        if tag not in parsed:
            parsed.append(tag)
    return parsed


def _next_note_id(ctx: ToolContext, notes: list[dict]) -> int:
    """Issue a note id that has never been used for this user.

    The counter alone would suffice for new users; the ``max`` over existing
    ids covers notes written before the counter existed, when ids were derived
    from the list length and a delete made the next save reuse an id.
    """
    highest_existing = max((n.get("id", 0) for n in notes if isinstance(n, dict)), default=0)
    last_issued = ctx.state.get(_NOTES_LAST_ID_KEY, 0)
    if not isinstance(last_issued, int):
        last_issued = 0
    note_id = max(highest_existing, last_issued) + 1
    ctx.state[_NOTES_LAST_ID_KEY] = note_id
    return note_id


# ── Session State: tracks what happened in this conversation ───────────


async def log_operation(ctx: ToolContext, operation: str, details: str) -> dict[str, Any]:
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

    log = _append_to_session_log(ctx, operation, details)

    return {
        "status": "success",
        "message": f"Logged operation '{operation}' to session.",
        "total_operations": len(log),
    }


async def get_session_summary(ctx: ToolContext) -> dict[str, Any]:
    """Returns a summary of the most recent operations performed in this session.

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


async def save_note(
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
    parsed_tags = _parse_tags(tags)
    if isinstance(parsed_tags, dict):
        return parsed_tags

    notes = list(ctx.state.get("user:notes", []))
    if len(notes) >= MAX_NOTES_PER_USER:
        return {
            "status": "error",
            "message": (
                f"You already have {len(notes)} notes (the limit is {MAX_NOTES_PER_USER}). "
                "Delete notes you no longer need before saving a new one."
            ),
        }

    note_id = _next_note_id(ctx, notes)
    note = {
        "id": note_id,
        "title": title,
        "content": content,
        "tags": parsed_tags,
        "created_at": _now(),
    }
    ctx.state["user:notes"] = [*notes, note]

    _append_to_session_log(ctx, "save_note", f"Saved note #{note_id}: {title}")

    return {
        "status": "success",
        "message": f"Note #{note_id} saved.",
        "note_id": note_id,
    }


async def list_notes(ctx: ToolContext, tag: str | None = None) -> dict[str, Any]:
    """Lists all saved notes for this user, optionally filtered by tag.

    Args:
        ctx: The tool context (injected by ADK).
        tag: Optional tag to filter by.

    Returns:
        A list of saved notes.
    """
    notes = ctx.state.get("user:notes", [])
    if tag:
        if err := validate_string(tag, "tag", max_len=50, pattern=TAG_PATTERN):
            return err
        notes = [n for n in notes if tag in n.get("tags", [])]

    return {
        "status": "success",
        "count": len(notes),
        "notes": notes,
    }


async def search_notes(ctx: ToolContext, query: str) -> dict[str, Any]:
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


async def delete_note(ctx: ToolContext, note_id: int) -> dict[str, Any]:
    """Deletes a saved note by ID.

    Args:
        ctx: The tool context (injected by ADK).
        note_id: The ID of the note to delete.

    Returns:
        Confirmation of deletion.
    """
    if err := validate_positive_int(note_id, "note_id"):
        return err

    notes = ctx.state.get("user:notes", [])
    # Remove exactly one note. Ids are unique for notes saved now, but notes
    # written before ids were made monotonic can share one, and deleting "#2"
    # must never take a second note with it.
    index = next((i for i, n in enumerate(notes) if n.get("id") == note_id), None)
    if index is None:
        return {"status": "error", "message": f"Note #{note_id} not found."}

    deleted = notes[index]
    ctx.state["user:notes"] = [*notes[:index], *notes[index + 1 :]]
    return {
        "status": "success",
        "message": f"Note #{note_id} ('{deleted.get('title', '')}') deleted.",
    }


# ── User Preferences: user-scoped settings ─────────────────────────────


async def set_preference(ctx: ToolContext, key: str, value: str) -> dict[str, Any]:
    """Sets a user preference that persists across sessions.

    Args:
        ctx: The tool context (injected by ADK).
        key: Preference name — letters, digits, '_', '.', '-' (e.g., "default_cluster").
        value: Preference value.

    Returns:
        Confirmation.
    """
    if err := validate_string(key, "key", max_len=64, pattern=PREFERENCE_KEY_PATTERN):
        return err
    if err := validate_string(value, "value", max_len=1000):
        return err

    prefs = dict(ctx.state.get("user:preferences", {}))
    if key not in prefs and len(prefs) >= MAX_PREFERENCES:
        return {
            "status": "error",
            "message": (
                f"You already have {len(prefs)} preferences (the limit is {MAX_PREFERENCES}). "
                "Overwrite an existing preference instead."
            ),
        }
    prefs[key] = value
    ctx.state["user:preferences"] = prefs

    return {
        "status": "success",
        "message": f"Preference '{key}' set to '{value}'.",
    }


async def get_preferences(ctx: ToolContext) -> dict[str, Any]:
    """Gets all user preferences.

    Args:
        ctx: The tool context (injected by ADK).

    Returns:
        All saved preferences.
    """
    prefs = ctx.state.get("user:preferences", {})
    return {"status": "success", "preferences": prefs}


# ── App State: shared across all users ─────────────────────────────────


@confirm("adds a bookmark that every user of this deployment will see")
async def add_team_bookmark(ctx: ToolContext, name: str, url: str) -> dict[str, Any]:
    """Adds (or updates, by name) a shared bookmark visible to all users.

    App-scoped state is the one place in this agent where one user's write
    reaches everyone else, so it is gated like any other shared mutation: RBAC
    requires ``operator``, and the call is confirmed before it runs.

    Args:
        ctx: The tool context (injected by ADK).
        name: Bookmark name.
        url: The http(s) URL to bookmark.

    Returns:
        Confirmation.
    """
    if err := validate_string(name, "name", max_len=200):
        return err
    if err := validate_string(url, "url", max_len=2048):
        return err
    if err := validate_url(url, "url"):
        return err

    bookmarks = list(ctx.state.get("app:bookmarks", []))
    # Re-adding a name replaces its URL rather than stacking duplicates.
    existing = next(
        (i for i, b in enumerate(bookmarks) if str(b.get("name", "")).lower() == name.lower()),
        None,
    )
    entry = {"name": name, "url": url}
    if existing is not None:
        bookmarks[existing] = entry
        verb = "updated"
    elif len(bookmarks) >= MAX_TEAM_BOOKMARKS:
        return {
            "status": "error",
            "message": (
                f"The team already has {len(bookmarks)} bookmarks "
                f"(the limit is {MAX_TEAM_BOOKMARKS})."
            ),
        }
    else:
        bookmarks.append(entry)
        verb = "added"
    ctx.state["app:bookmarks"] = bookmarks

    return {
        "status": "success",
        "message": f"Team bookmark '{name}' {verb}.",
    }


async def list_team_bookmarks(ctx: ToolContext) -> dict[str, Any]:
    """Lists all shared team bookmarks.

    Args:
        ctx: The tool context (injected by ADK).

    Returns:
        All team bookmarks.
    """
    bookmarks = ctx.state.get("app:bookmarks", [])
    return {"status": "success", "count": len(bookmarks), "bookmarks": bookmarks}
