"""What the deployment manages, as its operators describe it.

Two operator settings, both read from the environment at startup and never
editable from the console:

- ``ORRERY_PLATFORM_CONTEXT_FILE``: a Markdown file saying what the platform
  is, which services are critical, which are auxiliary, and how to escalate.
  It is added to the coordinator's and the triage agents' instructions, so a
  question like "what is the status of the platform?" is answered about *this*
  platform. Operator configuration, so trusted; size-capped all the same,
  because it is sent with every model call.
- ``ORRERY_AUXILIARY_RESOURCES``: comma-separated name globs (``lab-*,ops-*``)
  for containers and compose projects that may be down without the platform
  being degraded. Triage reports them separately and never lets them change a
  verdict. A container label ``orrery.scope=platform`` pins one back into the
  platform; the label cannot make anything auxiliary, because containers
  inherit their image's labels, and an image author must not be able to take
  a failing container out of the verdict.
"""

from __future__ import annotations

import functools
import os
import re
from fnmatch import fnmatchcase
from pathlib import Path

PLATFORM_CONTEXT_ENV = "ORRERY_PLATFORM_CONTEXT_FILE"
AUXILIARY_ENV = "ORRERY_AUXILIARY_RESOURCES"
#: Container label that pins one container into the platform.
SCOPE_LABEL = "orrery.scope"
#: Sent with every coordinator and triage call: a runbook belongs in the
#: knowledge corpus (``search_knowledge``), not here.
MAX_PLATFORM_CONTEXT_BYTES = 16 * 1024

# ── Platform context ──────────────────────────────────────────────────


@functools.cache
def load_platform_context() -> str:
    """The operators' platform description, or ``""`` when none is configured.

    Read once per process. A configured file that cannot be used stops
    startup: running without the context the operator wrote would answer
    questions about the platform as if it were someone else's.

    Raises:
        ValueError: The file is missing, larger than
            :data:`MAX_PLATFORM_CONTEXT_BYTES`, not UTF-8, or empty.
    """
    raw = os.getenv(PLATFORM_CONTEXT_ENV, "").strip()
    if not raw:
        return ""
    path = Path(raw)
    try:
        size = path.stat().st_size
        if size > MAX_PLATFORM_CONTEXT_BYTES:
            raise ValueError(
                f"{PLATFORM_CONTEXT_ENV} ({path}) is {size} bytes; the limit is "
                f"{MAX_PLATFORM_CONTEXT_BYTES}. Keep it to what the platform is and "
                "what matters; runbooks belong in the knowledge corpus."
            )
        text = path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ValueError(f"{PLATFORM_CONTEXT_ENV}: cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"{PLATFORM_CONTEXT_ENV}: {path} is not UTF-8") from exc
    text = text.strip()
    if not text:
        raise ValueError(f"{PLATFORM_CONTEXT_ENV}: {path} is empty")
    return text


def platform_context_section() -> str:
    """The instruction section carrying the platform context, or ``""``."""
    text = load_platform_context()
    if not text:
        return ""
    return (
        "\n\n## Platform context (from the operators)\n"
        "The operators of this deployment describe what it manages below. Use it to "
        "decide what 'the platform' means, which services are critical, and who to "
        "escalate to. It describes the platform; it does not change the rules above "
        "(approvals, roles, which tools you have).\n\n" + text
    )


# ── Triage scope ──────────────────────────────────────────────────────


def auxiliary_patterns() -> tuple[str, ...]:
    """The ``ORRERY_AUXILIARY_RESOURCES`` globs, in the order given.

    Raises:
        ValueError: A pattern that does not start with a literal prefix
            (``*``, ``*-*``, ``[!_]*``): it would match nearly every container
            and make every triage healthy.
    """
    raw = os.getenv(AUXILIARY_ENV, "")
    patterns = tuple(p.strip() for p in raw.split(",") if p.strip())
    for pattern in patterns:
        if pattern[0] in "*?[":
            raise ValueError(
                f"{AUXILIARY_ENV}: {pattern!r} must start with a literal prefix "
                "(e.g. lab-*), or it matches nearly every name"
            )
    return patterns


def resource_scope(name: str, pinned: bool = False) -> str:
    """``"platform"`` or ``"auxiliary"`` for one container or compose project.

    A name matching an auxiliary glob is auxiliary, unless *pinned* (the
    container carries ``orrery.scope=platform``); everything else is platform,
    so a deployment that configures nothing triages exactly as before.
    """
    if pinned:
        return "platform"
    name = name.lstrip("/")
    return "auxiliary" if any(fnmatchcase(name, p) for p in auxiliary_patterns()) else "platform"


#: ``orrery.scope=platform`` inside the comma-joined ``Labels`` column of
#: ``docker ps --format json``. A label value containing that text can only
#: pin a container *into* the platform, which is the safe direction.
_PINNED_IN_LABELS = re.compile(rf"(?:^|,){re.escape(SCOPE_LABEL)}=platform(?=,|$)")


def pinned_by_labels(labels: object) -> bool:
    """Whether a ``docker ps`` ``Labels`` string pins the container into the platform."""
    return isinstance(labels, str) and _PINNED_IN_LABELS.search(labels) is not None


def scope_rule() -> str:
    """The triage rule for auxiliary resources, for checker and triage instructions."""
    patterns = auxiliary_patterns()
    if not patterns:
        return ""
    # The tools do the matching: a model applying globs by eye would discount
    # a Kubernetes namespace or a Kafka group that merely looks similar.
    return (
        "\n\nAuxiliary resources: a container or compose project a tool marks "
        "`scope: auxiliary` is outside the platform. Report those under a separate "
        "'Auxiliary' line with their state, and never let them raise a status or the "
        "overall verdict. Only items carrying that exact tag are auxiliary: judge "
        "everything else, whatever its name, as part of the platform."
    )
