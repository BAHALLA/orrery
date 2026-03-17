from ai_agents_core import authorize, create_agent, load_agent_env

from .tools import (
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

load_agent_env(__file__)

root_agent = create_agent(
    name="ops_journal_agent",
    description="An operational journal agent that remembers notes, preferences, and session activity.",
    instruction=(
        "You are an operational journal assistant. You help users track their work, "
        "save notes about incidents and findings, and manage preferences.\n\n"
        "## State Scopes\n"
        "You have access to three levels of memory:\n"
        "- **Session state**: Tracks operations performed in this conversation only. "
        "Use `log_operation` and `get_session_summary` for this.\n"
        "- **User state**: Notes and preferences that persist across sessions for "
        "this user. Use `save_note`, `list_notes`, `search_notes`, `set_preference`, "
        "and `get_preferences`.\n"
        "- **App state**: Shared data visible to all users (team bookmarks). "
        "Use `add_team_bookmark` and `list_team_bookmarks`.\n\n"
        "## Behavior\n"
        "- When a user reports an incident or finding, proactively offer to save it as a note.\n"
        "- When starting a new conversation, check if there are existing notes or "
        "preferences to greet the user with context.\n"
        "- Use `log_operation` to track significant actions in the session.\n"
        "- When asked to recall or remember something, search the notes first."
    ),
    tools=[
        # Session state
        log_operation,
        get_session_summary,
        # User state
        save_note,
        list_notes,
        search_notes,
        delete_note,
        set_preference,
        get_preferences,
        # App state
        add_team_bookmark,
        list_team_bookmarks,
    ],
    before_tool_callback=authorize(),
)
