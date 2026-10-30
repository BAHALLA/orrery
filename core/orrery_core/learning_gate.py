"""The learning sandbox's gate (AEP-032): who may run with ``ORRERY_LEARNING_MODE``.

Learning mode withholds an exercise's answers, and only the HTTP server's reply
path is built to do that (the answer guard, the held stream, the narrowed
endpoints). So the flag is refused by default and accepted only by a process
that *activated* it, and only ``orrery_assistant.app`` does, as its first
statement:

- :func:`check` is called by the builders every surface goes through:
  ``create_agent()``, ``default_plugins()``, ``build_chat_agent()`` and
  ``AgentGateway``. With the flag set in a process that did not activate, each
  raises, so the Slack and Google Chat bots, ``adk web``, the CLI, the batch
  triage, a specialist run on its own and any future entry point refuse the
  flag without code of their own.
- :func:`activate_http_server` runs the sandbox's preconditions and loads the
  learning package. It must run before the agent tree is built, which happens
  at import, hence "first statement".

This module never imports :mod:`orrery_core.learning`: with the flag unset,
nothing of learning mode is loaded at all.
"""

from __future__ import annotations

from typing import Any

from .env import env_flag

LEARNING_ENV = "ORRERY_LEARNING_MODE"

#: The activated learning runtime (``orrery_core.learning.runtime.Runtime``),
#: or ``None``. Set once, by :func:`activate_http_server`.
_runtime: Any = None


class LearningModeRefused(RuntimeError):
    """``ORRERY_LEARNING_MODE`` is set where it cannot be honoured."""


def requested() -> bool:
    """Whether the deployment asked for learning mode. A typo raises (``env_flag``)."""
    return env_flag(LEARNING_ENV, default=False)


def runtime() -> Any:
    """The activated learning runtime, or ``None`` in a normal process."""
    return _runtime


def is_active() -> bool:
    return _runtime is not None


def check(where: str) -> None:
    """Refuse to build *where* when learning mode is requested but not activated."""
    if _runtime is None and requested():
        raise LearningModeRefused(
            f"{LEARNING_ENV} is set, but {where} is not running in the learning "
            "sandbox's HTTP server (orrery_assistant.app). Only that server withholds "
            "exercise answers; every other entry point refuses the flag rather than "
            "run unguarded."
        )


def activate_http_server() -> None:
    """Turn learning mode on for this process, if the deployment asked for it.

    Called by the HTTP server's module before anything else is built. Raises
    ``ValueError`` naming each unmet precondition, or ``ManifestError`` for a
    bad manifest; either stops startup. A no-op when the flag is unset.
    """
    global _runtime
    if _runtime is not None or not requested():
        return
    from .learning.runtime import Runtime

    _runtime = Runtime.from_env()
