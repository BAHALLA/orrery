"""RepeatGuardPlugin — stop a call that keeps failing the same way.

A model that gets an error back usually retries, and that is often right: the
clients here have little retry logic of their own, so a timeout, a ``502`` or a
token that had just expired reaches the model as an ordinary error. What is
never right is the *third* identical attempt. A call that has failed twice on
unchanged arguments, for the same reason both times, will not come good on the
third — it is missing an input only a human has (a namespace that does not
exist, a permission the service account lacks, a parameter the model keeps
guessing). Left alone, the model will spend a dozen calls proving it.

**Why the third and not the second.** Refusing the second call would break the
legitimate single retry of a transient fault. The second failure is what
separates the two cases: a transient fault does not usually recur verbatim, and
one that does is no longer transient.

**What "the same" means.** The same tool with the same arguments (a canonical
fingerprint), failing with the same *error signature* — the error text,
lower-cased, with digits stripped so a request id or timestamp does not make two
identical failures look different. A second failure with a different signature
is progress: it restarts the count, because the new error is the next thing to
fix. Arguments are part of the match on purpose: the circuit breaker already
covers "this tool is unhealthy", and blocking a good call because a different
bad one failed would be worse than the loop.

**One probe, then closed.** Conditions change mid-conversation — someone grants
the missing permission, creates the namespace. A guard that could never re-open
would have the agent insisting something is impossible after it was made
possible. So after a refusal the entry goes *half-open*, like a circuit breaker:
the next identical attempt runs as a probe. A probe that succeeds clears the
entry; one that fails the same way closes it for the rest of the session.

**Scope: the session**, not the run. The failure being caught spans turns — the
user says "try again", the model sends the identical call — and the arguments
are part of the key, so a session that genuinely changes something is never
held back by it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..reliability.resilience import ToolOutcome, classify_tool_outcome
from .run_scope import RunLedger, session_id

logger = logging.getLogger("orrery.plugins.repeat_guard")

#: The ``status`` the model sees in place of a refused repeat. Neutral for the
#: circuit breaker (the tool never ran): see ``_NEUTRAL_STATUSES``.
REPEAT_REFUSED_STATUS = "REPEATED_FAILURE"

#: Identical failures after which the next identical call is refused.
DEFAULT_MAX_IDENTICAL_FAILURES = 2

#: Characters of the error text kept in its signature.
_SIGNATURE_CHARS = 200
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


@dataclass
class _Entry:
    signature: str = ""
    failures: int = 0
    #: A refusal was issued; the next identical call runs as a probe.
    half_open: bool = False
    #: A probe failed the same way; refuse for the rest of the session.
    closed: bool = False


def fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    """A stable key for *tool_name* called with *args*."""
    try:
        canonical = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    except TypeError, ValueError:
        canonical = repr(sorted(args.items(), key=lambda kv: str(kv[0])))
    digest = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:24]
    return f"{tool_name}:{digest}"


def error_signature(result: Any) -> str:
    """Normalise a failed result's error text so recurrences compare equal."""
    text = ""
    for key in ("error", "message", "detail", "error_type"):
        value = result.get(key) if isinstance(result, dict) else getattr(result, key, None)
        if isinstance(value, str) and value:
            text = value
            break
    text = _DIGITS.sub("#", text.lower())
    return _SPACE.sub(" ", text).strip()[:_SIGNATURE_CHARS]


class RepeatGuardPlugin(BasePlugin):
    """Refuse a call that already failed identically ``max_failures`` times.

    Args:
        max_failures: Identical failures (same tool, arguments and error)
            after which the next identical call is refused. ``0`` disables.
    """

    def __init__(self, *, max_failures: int = DEFAULT_MAX_IDENTICAL_FAILURES) -> None:
        super().__init__(name="repeat_guard")
        self._max = max_failures
        self._sessions: RunLedger[dict[str, _Entry]] = RunLedger(dict)

    def _failures(self, context: Any) -> dict[str, _Entry] | None:
        """The session's failing calls. Only failing calls are ever stored."""
        session = session_id(context)
        return self._sessions.get(session) if session else None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        if self._max <= 0:
            return None
        failing = self._failures(tool_context)
        entry = failing.get(fingerprint(tool.name, tool_args)) if failing is not None else None
        if entry is None or entry.failures < self._max:
            return None
        if entry.half_open and not entry.closed:
            # This call is the probe: let it through, and judge it afterwards.
            entry.half_open = False
            return None
        if not entry.closed:
            entry.half_open = True
        logger.warning(
            "Refused '%s': failed %d times with identical arguments and error",
            tool.name,
            entry.failures,
        )
        return {
            "status": REPEAT_REFUSED_STATUS,
            "error": (
                f"'{tool.name}' has already failed {entry.failures} times with these "
                f"exact arguments, for the same reason each time ({entry.signature!r}), "
                "so it was not called again. Do not retry it unchanged. Either change "
                "something concrete (a different name, namespace, filter or time "
                "range), or tell the user what is missing and ask them for it — a "
                "call that fails the same way on unchanged arguments usually needs "
                "an input only a human has."
            ),
        }

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> None:
        if self._max <= 0:
            return None
        outcome = classify_tool_outcome(result)
        if outcome is ToolOutcome.IGNORE:
            return None  # a gate answered, including this plugin: nothing ran
        failing = self._failures(tool_context)
        if failing is None:
            return None
        key = fingerprint(tool.name, tool_args)
        if outcome is ToolOutcome.SUCCESS:
            failing.pop(key, None)
            return None
        entry = failing.setdefault(key, _Entry())
        signature = error_signature(result)
        if entry.failures and signature != entry.signature:
            # A different error is progress: restart the count on the new one.
            entry.failures = 0
            entry.half_open = entry.closed = False
        was_probe = entry.failures >= self._max
        entry.signature = signature
        entry.failures += 1
        if was_probe:
            entry.closed = True
        return None
