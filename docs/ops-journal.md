# ops-journal

An agent demonstrating ADK's **memory and state** patterns. It acts as an operational journal — saving notes, tracking session activity, and managing user preferences — all using different state scopes.

## Concepts Demonstrated

### State Scopes

ADK provides four state scopes, controlled by key prefixes:

| Prefix | Scope | Persists across sessions? | Shared across users? | Example |
|--------|-------|--------------------------|---------------------|---------|
| *(none)* | Session | No | No | `ctx.state["session_log"]` |
| `user:` | User | Yes | No | `ctx.state["user:notes"]` |
| `app:` | App | Yes | Yes | `ctx.state["app:bookmarks"]` |
| `temp:` | Temporary | No (not even persisted) | No | `ctx.state["temp:scratch"]` |

### How Tools Access State

Tools receive a `ToolContext` (alias for `Context`) as their first parameter. ADK injects it automatically — you just declare it:

```python
from google.adk.tools import ToolContext

def save_note(ctx: ToolContext, title: str, content: str) -> dict:
    notes = ctx.state.get("user:notes", [])   # read
    notes.append({"title": title, "content": content})
    ctx.state["user:notes"] = notes            # write
    return {"status": "success"}
```

The `ctx.state` object is **delta-aware**: changes are tracked and only the diff is persisted.

### Session Services

| Service | Storage | Use case |
|---------|---------|----------|
| `InMemorySessionService` | RAM | Development (`adk web` default) |
| `DatabaseSessionService` | SQLite / PostgreSQL / MySQL | Production |

With `adk web`, state resets on restart. Use `run_persistent.py` for true persistence:

```bash
make run-journal-persistent
```

This uses `DatabaseSessionService` with a local SQLite file (`ops_journal.db`), so `user:*` and `app:*` state survive across sessions and restarts.

## Tools

### Session State (current conversation only)

| Tool | Description |
|------|-------------|
| `log_operation` | Log an operation to the session activity log |
| `get_session_summary` | Get all operations performed in this session |

### User State (persists across sessions)

| Tool | Description |
|------|-------------|
| `save_note` | Save a note with optional tags |
| `list_notes` | List all notes, optionally filter by tag |
| `search_notes` | Search notes by keyword |
| `delete_note` | Delete a note by ID |
| `set_preference` | Save a user preference |
| `get_preferences` | Get all user preferences |

### App State (shared across all users)

| Tool | Description |
|------|-------------|
| `add_team_bookmark` | Add a shared bookmark |
| `list_team_bookmarks` | List all team bookmarks |

## Running

```bash
# ADK Dev UI (in-memory state — resets on restart)
make run-journal

# Terminal mode with SQLite persistence
make run-journal-persistent
```

## Things to Try

1. **Session tracking**: Ask the agent to do several things, then ask "what have we done so far?"
2. **Notes across sessions**: Save a note, type `new` to start a fresh session, then ask "do I have any notes?"  (requires persistent mode)
3. **User preferences**: Set a preference like "default_cluster = prod", start a new session, and check if it remembers
4. **Team bookmarks**: Add a bookmark — it'll be visible to any user
