from orrery_core import create_agent, load_agent_env
from orrery_core.security.guardrails import require_confirmation

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
        "You are the operational journal (SRE record-keeping). You save and retrieve "
        "notes about incidents and findings, track session activity, and manage "
        "preferences and team bookmarks.\n\n"
        "## State scopes — pick the right one\n"
        "- **Session** (this conversation only): log_operation, get_session_summary.\n"
        "- **User** (persists across sessions for this user): save_note, list_notes, "
        "search_notes, delete_note, set_preference, get_preferences.\n"
        "- **App** (shared with the whole team): add_team_bookmark, list_team_bookmarks.\n\n"
        "## Behavior\n"
        "- Asked to recall something? search_notes FIRST and answer only from what it "
        "returns — if nothing matches, say so; never invent a past finding or note.\n"
        "- When saving an incident note, capture the facts verbatim: what broke, "
        "evidence (exact names/numbers), what fixed it, and a date. Tag it so it can "
        "be found again.\n"
        "- After a write (save/delete/set), confirm what was written by echoing the "
        "tool's returned result, not your intent.\n"
        "- When a user reports a finding, offer once to save it — don't nag.\n"
        "- add_team_bookmark is visible to the whole team and needs confirmation: if it "
        "returns confirmation_required, relay that to the user and wait for their answer.\n"
        "- Answer directly; no filler."
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
    # Gates the @confirm tools (add_team_bookmark). Wired per agent rather than
    # as a plugin so it also holds inside AgentTool sub-sessions and `adk web`.
    before_tool_callback=require_confirmation(),
)
