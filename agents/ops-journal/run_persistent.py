"""Run ops-journal-agent with SQLite-backed persistent sessions.

Unlike `adk web` (which uses in-memory sessions that vanish on restart),
this runner persists all state to a local SQLite database. This means:
  - user:* state survives across sessions and restarts
  - app:* state is shared and persisted
  - session history is preserved

Usage:
    uv run python run_persistent.py
"""

import asyncio

from google.adk.runners import Runner
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.genai import types

from ops_journal_agent.agent import root_agent

APP_NAME = "ops_journal"
USER_ID = "default_user"
DB_URL = "sqlite:///ops_journal.db"


async def main():
    session_service = DatabaseSessionService(db_url=DB_URL)

    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )

    print("Ops Journal Agent (persistent mode)")
    print(f"Session: {session.id}")
    print(f"Database: {DB_URL}")
    print("Type 'quit' to exit, 'new' for a new session.\n")

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() == "quit":
            break
        if user_input.lower() == "new":
            session = await session_service.create_session(
                app_name=APP_NAME,
                user_id=USER_ID,
            )
            print(f"\n--- New session: {session.id} ---\n")
            continue

        message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_input)],
        )

        response_text = ""
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session.id,
            new_message=message,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        response_text += part.text

        if response_text:
            print(f"\nAgent: {response_text}\n")


if __name__ == "__main__":
    asyncio.run(main())
