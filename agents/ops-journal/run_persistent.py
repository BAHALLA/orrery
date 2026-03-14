"""Run ops-journal-agent with SQLite-backed persistent sessions.

Usage:
    uv run python run_persistent.py
"""

import asyncio

from ai_agents_core import run_persistent
from ops_journal_agent.agent import root_agent

if __name__ == "__main__":
    asyncio.run(run_persistent(root_agent, app_name="ops_journal"))
