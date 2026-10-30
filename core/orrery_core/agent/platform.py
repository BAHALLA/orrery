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
  for resources that may be down without the platform being degraded. A
  container label ``orrery.scope=auxiliary`` (or ``=platform``, which wins
  over a glob) says the same per container. Triage reports auxiliary
  resources separately and never lets them change a verdict.
"""

from __future__ import annotations

import functools
import os
import re
from fnmatch import fnmatchcase
from pathlib import Path

PLATFORM_CONTEXT_ENV = "ORRERY_PLATFORM_CONTEXT_FILE"
AUXILIARY_ENV = "ORRERY_AUXILIARY_RESOURCES"
#: Container label that places one resource explicitly.
SCOPE_LABEL = "orrery.scope"
SCOPES = ("platform", "auxiliary")
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
    """The ``ORRERY_AUXILIARY_RESOURCES`` globs, in the order given."""
    raw = os.getenv(AUXILIARY_ENV, "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def resource_scope(name: str, label: str | None = None) -> str:
    """``"platform"`` or ``"auxiliary"`` for one resource.

    An explicit ``orrery.scope`` label wins; otherwise a name matching one of
    the auxiliary globs is auxiliary; everything else is platform, so a
    deployment that configures nothing triages exactly as before.
    """
    if label in SCOPES:
        return label
    name = name.lstrip("/")
    return "auxiliary" if any(fnmatchcase(name, p) for p in auxiliary_patterns()) else "platform"


#: ``orrery.scope=<value>`` inside the comma-joined ``Labels`` column of
#: ``docker ps --format json``.
_SCOPE_IN_LABELS = re.compile(rf"(?:^|,){re.escape(SCOPE_LABEL)}=(platform|auxiliary)(?=,|$)")


def scope_from_labels(labels: object) -> str | None:
    """The ``orrery.scope`` value in a ``docker ps`` ``Labels`` string, if any."""
    if not isinstance(labels, str):
        return None
    match = _SCOPE_IN_LABELS.search(labels)
    return match.group(1) if match else None


def scope_rule() -> str:
    """The triage rule for auxiliary resources, for checker and triage instructions."""
    patterns = auxiliary_patterns()
    named = (
        " Resources named like " + ", ".join(f"`{p}`" for p in patterns) + " are auxiliary too."
        if patterns
        else ""
    )
    return (
        "Auxiliary resources: an item a tool marks `scope: auxiliary` is outside the "
        f"platform.{named} Report auxiliary items under a separate 'Auxiliary' line with "
        "their state, and never let them raise a status or the overall verdict: the "
        "platform is degraded only by platform resources."
    )
