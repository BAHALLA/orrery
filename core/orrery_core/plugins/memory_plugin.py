"""MemoryPlugin — auto-saves sessions to long-term memory after the root agent runs."""

from __future__ import annotations

import logging

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins.base_plugin import BasePlugin

logger = logging.getLogger("orrery.plugins")


class MemoryPlugin(BasePlugin):
    """Auto-saves sessions to long-term memory after root agent completes.

    Only saves sessions with at least ``min_events`` events to avoid
    polluting memory with trivial single-turn interactions.

    Requires a ``memory_service`` to be configured on the Runner.

    Args:
        min_events: Minimum event count before a session is worth saving.
    """

    def __init__(self, min_events: int = 4) -> None:
        super().__init__(name="memory")
        self._min_events = min_events

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> None:
        """Save session to memory after the root agent finishes."""
        inv = callback_context._invocation_context  # noqa: SLF001

        # Only fire for the root agent. In ADK 2.0 inv.agent is typed
        # BaseNode | None, so guard before comparing names.
        if inv.agent is None or agent.name != inv.agent.name:
            return

        memory_service = inv.memory_service
        if memory_service is None:
            return

        session = inv.session
        if len(session.events) < self._min_events:
            logger.debug(
                "Skipping memory save for session %s (%d events < %d min)",
                session.id,
                len(session.events),
                self._min_events,
            )
            return

        await memory_service.add_session_to_memory(session)
        logger.debug(
            "Saved session %s to memory (%d events)",
            session.id,
            len(session.events),
        )
        return
