"""The activated learning sandbox: what an HTTP server in learning mode holds.

Built once, by ``learning_gate.activate_http_server()``, after the
preconditions pass and the manifest loads. The serving layer asks it for its
plugins and for the narrowed answers of the endpoints that could otherwise
show a value (AEP-032 §4–§5).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins.base_plugin import BasePlugin

from .config import SPAN_CONTENT_ENV, check_preconditions
from .guard import AnswerGuardPlugin
from .manifest import ManifestStore

logger = logging.getLogger("orrery.learning")


@dataclass(frozen=True)
class Runtime:
    """The sandbox's state for one process."""

    store: ManifestStore

    @classmethod
    def from_env(cls) -> Runtime:
        """Check the preconditions, then load the manifest. Either failure raises."""
        # ADK reads an unset switch as "copy content into spans"; the server
        # sets the same default later, but the check runs before it.
        os.environ.setdefault(SPAN_CONTENT_ENV, "false")
        check_preconditions()
        store = ManifestStore.from_env()
        manifest = store.current()
        logger.warning(
            "Learning sandbox mode is ON (scenario %s, %d target(s)): replies that "
            "state an exercise's answer are withheld",
            manifest.scenario_id,
            len(manifest.targets),
        )
        return cls(store=store)

    def scenario_id(self) -> str:
        return self.store.current().scenario_id

    def level_of(self, callback_context: CallbackContext) -> int:
        """The learner's disclosure level. Phase 1: the manifest's default for everyone."""
        del callback_context
        return self.store.current().default_disclosure

    def plugins(self, root_agent_name: Callable[[], str]) -> list[BasePlugin]:
        """The plugins a gateway adds to its Runner in learning mode."""
        return [
            AnswerGuardPlugin(
                store=self.store,
                root_agent_name=root_agent_name,
                level_of=self.level_of,
            )
        ]
