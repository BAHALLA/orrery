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
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins.base_plugin import BasePlugin

from .config import SPAN_CONTENT_ENV, check_preconditions
from .guard import AnswerGuardPlugin
from .hints import DisclosureInstructionPlugin, HintStore, level_from_state, record_hint, state_for
from .manifest import ManifestStore

logger = logging.getLogger("orrery.learning")


@dataclass(frozen=True)
class Runtime:
    """The sandbox's state for one process."""

    store: ManifestStore
    hints: HintStore = field(default_factory=HintStore)

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

    def default_level(self) -> int:
        return self.store.current().default_disclosure

    def level_of(self, callback_context: CallbackContext) -> int:
        """The level the server stamped for this turn (the default if none)."""
        return level_from_state(callback_context.state, self.scenario_id(), self.default_level())

    def level_for(self, subject: str) -> int:
        """A learner's current level in the scenario in force."""
        return self.hints.level(subject, self.scenario_id(), self.default_level())

    def turn_state(self, subject: str) -> dict[str, Any]:
        """What a turn's ``state_delta`` carries: the level, marked as the server's."""
        return state_for(self.scenario_id(), self.level_for(subject))

    def take_hint(self, subject: str) -> tuple[int, int]:
        """Raise *subject*'s level by one (capped), audit it, return ``(old, new)``."""
        scenario = self.scenario_id()
        old, new = self.hints.raise_level(subject, scenario, self.default_level())
        if new != old:
            record_hint(subject, scenario, old, new)
        return old, new

    def plugins(self, root_agent_name: Callable[[], str]) -> list[BasePlugin]:
        """The plugins a gateway adds to its Runner in learning mode."""
        return [
            AnswerGuardPlugin(
                store=self.store,
                root_agent_name=root_agent_name,
                level_of=self.level_of,
            ),
            DisclosureInstructionPlugin(root_agent_name=root_agent_name, level_of=self.level_of),
        ]
