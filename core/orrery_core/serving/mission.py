"""What this console's visitors are here to do: a lab's brief, shown in the console.

A lab's learner arrives with a task, and a console that opens on "What would you
like to check?" with generic suggestions knows nothing about it. These may even
answer the exercise. So an operator can name the situation, the goal, where the
tasks live and a few places to start (``ORRERY_CONSOLE_MISSION_FILE``, JSON).

It is display only. The brief never reaches the model (a starter the learner
clicks is sent as their own message), nothing here grades or tracks progress
(AEP-032 leaves that to the lab platform), and every caller of ``GET /me`` reads
the same text, so it must not hold an exercise's answer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..safe_url import split_url

MISSION_FILE_ENV = "ORRERY_CONSOLE_MISSION_FILE"
#: The same limit as ``ORRERY_PLATFORM_CONTEXT_FILE``: a brief, not a document.
MAX_MISSION_BYTES = 16 * 1024

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class MissionStarter(BaseModel):
    """A place to start: a card that sends *prompt* as the learner's message."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, hide_input_in_errors=True
    )

    title: str = Field(min_length=1, max_length=60)
    prompt: str = Field(min_length=1, max_length=500)


class Mission(BaseModel):
    """The brief. Shown as plain text, never as markup."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, str_strip_whitespace=True, hide_input_in_errors=True
    )

    title: str = Field(min_length=1, max_length=120)
    situation: str = Field(min_length=1, max_length=1000)
    goal: str = Field(min_length=1, max_length=500)
    tasks_url: str | None = None
    tasks_label: str = Field(default="Open the tasks", min_length=1, max_length=40)
    starters: tuple[MissionStarter, ...] = Field(default=(), max_length=4)

    @field_validator("tasks_url")
    @classmethod
    def _safe_link(cls, url: str | None) -> str | None:
        """Only a link a browser can follow safely; messages never repeat the URL."""
        if url is None:
            return None
        # urlsplit drops these and a browser may too, so what is checked would
        # not be the link that is served.
        if " " in url or not url.isprintable():
            raise ValueError("tasks_url must not hold spaces or control characters")
        parts = split_url(url, "tasks_url")
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() != "https" and not (
            parts.scheme.lower() == "http" and host in _LOOPBACK
        ):
            raise ValueError("tasks_url must be https (plain http only on loopback)")
        if not host:
            raise ValueError("tasks_url has no host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("tasks_url must not hold credentials")
        return url


def load_mission(path: str) -> Mission:
    """The brief in *path*. Raises ``ValueError``, so a bad file stops startup."""
    target = Path(path)
    try:
        with target.open("rb") as file:
            raw = file.read(MAX_MISSION_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"{MISSION_FILE_ENV}: can't read {path}: {exc.strerror}") from exc
    if len(raw) > MAX_MISSION_BYTES:
        raise ValueError(f"{MISSION_FILE_ENV}: {path} is over {MAX_MISSION_BYTES} bytes")
    try:
        return Mission.model_validate(json.loads(raw.decode("utf-8")))
    except UnicodeDecodeError as exc:
        raise ValueError(f"{MISSION_FILE_ENV}: {path} isn't UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{MISSION_FILE_ENV}: {path} isn't valid JSON: {exc.msg}") from exc
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'file'}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(f"{MISSION_FILE_ENV}: {path}: {problems}") from exc


def mission_from_env() -> Mission | None:
    path = os.getenv(MISSION_FILE_ENV, "").strip()
    return load_mission(path) if path else None
