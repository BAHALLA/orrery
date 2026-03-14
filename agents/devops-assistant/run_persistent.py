"""Run devops-assistant with SQLite-backed persistent sessions.

Usage:
    uv run python run_persistent.py
"""

import asyncio

from ai_agents_core import run_persistent
from devops_assistant.agent import root_agent

if __name__ == "__main__":
    asyncio.run(run_persistent(root_agent, app_name="devops_assistant"))
