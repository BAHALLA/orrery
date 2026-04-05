"""Reusable persistent runner for CLI-based agent interaction.

Wraps the ADK Runner with DatabaseSessionService so that session state,
user notes, and app-wide data survive across restarts.
"""

from __future__ import annotations

from collections.abc import Sequence

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.genai import types

from .rbac import set_user_role


async def run_persistent(
    agent: Agent,
    *,
    app_name: str,
    db_url: str | None = None,
    user_id: str = "default_user",
    plugins: Sequence[BasePlugin] | None = None,
) -> None:
    """Run an agent in a persistent CLI loop with SQLite-backed sessions.

    Args:
        agent: The root agent to run.
        app_name: Application name for session scoping.
        db_url: SQLAlchemy database URL. Defaults to ``sqlite:///{app_name}.db``.
        user_id: User ID for session scoping.
        plugins: Optional list of ADK plugins for cross-cutting concerns.
            Use ``default_plugins()`` for the standard set.
    """
    resolved_db_url = db_url or f"sqlite:///{app_name}.db"

    session_service = DatabaseSessionService(db_url=resolved_db_url)

    # Wrap the agent in an ADK App so plugins can be passed via the supported
    # `app` argument (the `plugins=` kwarg on Runner is deprecated).
    app = App(
        name=app_name,
        root_agent=agent,
        plugins=list(plugins) if plugins else [],
    )
    runner = Runner(app=app, session_service=session_service)

    initial_state: dict[str, object] = {}
    set_user_role(initial_state, "admin")  # CLI user gets admin (local dev)
    session = await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        state=initial_state,
    )

    print(f"{agent.name} (persistent mode)")
    print(f"Session: {session.id}")
    print(f"Database: {resolved_db_url}")
    print("Type 'quit' to exit, 'new' for a new session.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue
        if user_input.lower() == "quit":
            break
        if user_input.lower() == "new":
            new_state: dict[str, object] = {}
            set_user_role(new_state, "admin")
            session = await session_service.create_session(
                app_name=app_name,
                user_id=user_id,
                state=new_state,
            )
            print(f"\n--- New session: {session.id} ---\n")
            continue

        message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_input)],
        )

        response_text = ""
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=message,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        response_text += part.text

        if response_text:
            print(f"\nAgent: {response_text}\n")
