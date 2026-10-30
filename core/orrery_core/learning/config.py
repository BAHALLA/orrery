"""What a deployment must look like before learning mode will start (AEP-032 §1).

Each condition closes a path that would show a learner text the answer guard
never saw, or that would split the guard's state. They are checked together, so
one failed start names everything that is wrong rather than one item per try.
"""

from __future__ import annotations

import logging
import os

from ..env import env_flag

#: The loggers that log every model response at DEBUG.
_RESPONSE_LOGGERS = ("google_adk", "LiteLLM")


def _flag(name: str, *, default: bool) -> bool | None:
    """The flag's value, or ``None`` when it is not a boolean (reported, not raised)."""
    try:
        return env_flag(name, default=default)
    except ValueError:
        return None


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def unmet_preconditions() -> list[str]:
    """Every precondition the current environment fails, as one line each."""
    unmet: list[str] = []

    def require(ok: bool, message: str) -> None:
        if not ok:
            unmet.append(message)

    require(
        _flag("AUTH_ENABLED", default=True) is True,
        "AUTH_ENABLED must be true: hints and answers are per verified person",
    )
    require(
        bool(_env("ORRERY_HUMAN_CLIENTS")),
        "ORRERY_HUMAN_CLIENTS must be set: only verified humans are learners (ADR-006)",
    )
    require(
        _env("ORRERY_AUTONOMY_LEVEL").upper() == "L2",
        "ORRERY_AUTONOMY_LEVEL must be L2: the sandbox is read-only",
    )
    for name, why in (
        ("ORRERY_A2A_ENABLED", "machine callers sit outside the learner's sessions"),
        ("ORRERY_STUDIO_ENABLED", "a Studio run builds its own plugins, outside the guard"),
        ("ORRERY_ADMIN_EDITING", "a catalog edit rebuilds what learners talk to"),
        ("ORRERY_MULTI_REPLICA", "the manifest and hint state are per process"),
    ):
        require(_flag(name, default=False) is False, f"{name} must be off: {why}")
    require(
        _env("WEB_CONCURRENCY") in ("", "1"),
        "WEB_CONCURRENCY must be unset or 1: several workers would split the guard's state",
    )
    require(
        _flag("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", default=False) is False,
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS must be off: ADK traces a model response "
        "before the answer guard sees it",
    )
    level = _env("LOG_LEVEL").upper()
    require(
        level not in ("DEBUG", "NOTSET"),
        "LOG_LEVEL must be INFO or above: ADK logs every model response at DEBUG",
    )
    for name in _RESPONSE_LOGGERS:
        logger_level = logging.getLogger(name).level
        require(
            logger_level == logging.NOTSET or logger_level > logging.DEBUG,
            f"the {name} logger must not be set to DEBUG: it logs every model response",
        )
    return unmet


def check_preconditions() -> None:
    """Raise ``ValueError`` listing every unmet precondition."""
    if unmet := unmet_preconditions():
        raise ValueError(
            "ORRERY_LEARNING_MODE cannot start:\n" + "\n".join(f"  - {u}" for u in unmet)
        )
