"""The answer guard (AEP-032 §4): the coordinator never states an exercise's answer.

A coaching prompt is a request to a model that has just read the answer from a
tool, and the coordinator can route around a coach to a specialist with no such
instruction. So the control sits on the one thing the learner reads: the
coordinator's own text. It is deterministic. The manifest says what the
answers are, and :func:`states_value` decides whether a reply says one.

Two choices are load-bearing:

- **In place, returning None.** ADK's after-model chain stops at the first
  plugin that returns a response, and ``TokenBudgetPlugin`` settles a call's
  tokens in that same chain. A replacement would leave the budget unsettled, so
  the guard edits ``llm_response`` and every other plugin still runs. ADK
  persists and replies from the same object, so the stored transcript holds the
  nudge, not the original.
- **The gateway's root, by name.** Every specialist runs in an ``AgentTool``
  sub-runner that inherits the plugins, where it is the root of its own
  invocation. Only text from the agent the gateway serves reaches the learner,
  so that is the only text checked (and rewriting a specialist's answer would
  only make the coordinator ask again).

What it does not catch, by design and measured by the eval rather than
promised: paraphrased approximations ("about five thousand"), unusual
encodings (digits spelled one by one, reversed, hex, "5099+1"), and a value
split across two responses.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from ..observability.audit import audit_event
from .manifest import GuardedValue, ManifestStore

logger = logging.getLogger("orrery.learning.guard")

#: The level at which target values are no longer withheld.
REVEAL_LEVEL = 3

# ── Matching ─────────────────────────────────────────────────────────

# Thousands separators people and models write: comma, dot, apostrophe,
# underscore, and the space variants (plain, no-break, narrow no-break, thin).
_SEPARATORS = ",.'_    "
_SEP = "[" + re.escape(_SEPARATORS) + "]?"
# Not preceded by a letter or digit (so v5100 or 25100 is another token), nor
# by a digit group (1,5100); not followed by a digit or a decimal part. A unit
# may follow directly: "5100ms" states 5100.
_BEFORE = r"(?<![0-9A-Za-z])(?<![0-9][.,])"
_AFTER = r"(?![0-9])(?![.,][0-9])"

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = ((10**9, "billion"), (10**6, "million"), (1000, "thousand"))


def _under_thousand(n: int) -> list[str]:
    words: list[str] = []
    if n >= 100:
        words += [_ONES[n // 100], "hundred"]
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10])
        n %= 10
        if n:
            words.append(_ONES[n])
    elif n:
        words.append(_ONES[n])
    return words


def number_words(n: int) -> str:
    """English words for a non-negative integer below 10**12, "and" omitted."""
    if n == 0:
        return "zero"
    words: list[str] = []
    for scale, name in _SCALES:
        if n >= scale:
            words += [*_under_thousand(n // scale), name]
            n %= scale
    words += _under_thousand(n)
    return " ".join(words)


def _normalise_words(text: str) -> str:
    """Lowercase, hyphens as spaces, "and" dropped, single spaces."""
    text = re.sub(r"[-‐‑‒–]", " ", text.lower())
    text = re.sub(r"\band\b", " ", text)
    return " " + re.sub(r"[^a-z]+", " ", text).strip() + " "


def _digits_pattern(n: int) -> str:
    """5100 with or without one consistent thousands separator."""
    digits = str(n)
    groups = []
    while digits:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    if len(groups) == 1:
        return re.escape(groups[0])
    sep = f"(?P<sep>{_SEP})"
    rest = "".join(f"(?P=sep){re.escape(g)}" for g in groups[2:])
    return re.escape(groups[0]) + sep + re.escape(groups[1]) + rest


def _abbreviations(n: int) -> list[tuple[float, str]]:
    """(scaled value, suffix pattern) pairs a reply might round *n* to."""
    out = []
    for scale, suffix in ((10**9, "b(?:n|illion)?"), (10**6, "m(?:n|illion)?"), (10**3, "k")):
        if n >= scale:
            out.append((n / scale, suffix))
    return out


def _states_number(text: str, value: float) -> bool:
    magnitude = abs(value)
    if float(magnitude).is_integer():
        n = int(magnitude)
        if re.search(_BEFORE + _digits_pattern(n) + _AFTER, text):
            return True
        if n < 10**12 and f" {number_words(n)} " in _normalise_words(text):
            return True
        # "5.1k", "5,1 K", "5k", "1.2M": the value rounded to the decimals
        # written, and close enough to be the value (1k is not 1,450).
        for scaled, suffix in _abbreviations(n):
            pattern = _BEFORE + r"(\d+(?:[.,]\d+)?)\s?" + f"(?:{suffix})" + r"(?![A-Za-z])"
            for match in re.finditer(pattern, text, re.IGNORECASE):
                written_text = match.group(1).replace(",", ".")
                decimals = len(written_text.partition(".")[2])
                written = float(written_text)
                if round(scaled, decimals) == written and abs(written - scaled) <= 0.1 * scaled:
                    return True
        return False
    whole, _, frac = repr(magnitude).partition(".")
    body = _digits_pattern(int(whole)) + "[.,]" + re.escape(frac)
    return re.search(_BEFORE + body + _AFTER, text) is not None


def states_value(text: str, value: int | float | str) -> bool:
    """Whether *text* states *value* in a form the guard recognises."""
    if isinstance(value, str):
        return value.strip().lower() in text.lower()
    return _states_number(text, value)


# ── The plugin ───────────────────────────────────────────────────────

_NUDGES = {
    0: "Think about which signal would show {descriptor}: an alert, a metric, logs?",
    1: "Look for where {descriptor} is reported: which topic, group or alert carries it?",
    2: "You're close: compare the numbers that make up {descriptor} yourself.",
}
_FALLBACK = "I can't give that value yet. Work it out from the evidence, or ask for a hint."


def nudge(value: GuardedValue, level: int) -> str:
    """What the learner reads instead of the answer."""
    hint = _NUDGES.get(level, _NUDGES[2]).format(descriptor=value.descriptor)
    return f"I can't give that value yet — it's part of your exercise. {hint}"


def _text_of(parts: list[types.Part]) -> str:
    return "\n".join(p.text for p in parts if p.text and p.thought is not True)


class AnswerGuardPlugin(BasePlugin):
    """Rewrites the coordinator's text when it states a guarded value.

    Args:
        store: The manifest in force.
        root_agent_name: The name of the agent the gateway serves, read per
            call so it follows ``replace_root_agent()``.
        level_of: The learner's disclosure level for this call.
    """

    def __init__(
        self,
        *,
        store: ManifestStore,
        root_agent_name: Callable[[], str],
        level_of: Callable[[CallbackContext], int],
    ) -> None:
        super().__init__(name="learning_answer_guard")
        self._store = store
        self._root = root_agent_name
        self._level_of = level_of

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        # Partial chunks are never forwarded in learning mode (the SSE layer
        # holds them); the final aggregated response is what gets checked.
        if llm_response.partial is True or callback_context.agent_name != self._root():
            return None
        content = llm_response.content
        parts = list(content.parts or []) if content else []
        text = _text_of(parts)
        if not text:
            return None
        level = self._level_of(callback_context)
        if level >= REVEAL_LEVEL:
            return None
        stated = [v for v in self._store.guarded_values() if states_value(text, v.value)]
        if not stated:
            return None

        first = stated[0]
        replacement = nudge(first, level)
        if any(states_value(replacement, v.value) for v in self._store.guarded_values()):
            replacement = _FALLBACK
        # Keep tool calls (a mixed text-and-call response still runs its tools);
        # drop every text and thought part. Edited in place: see the module doc.
        kept = [p for p in parts if not p.text and p.thought is not True]
        assert content is not None  # parts came from it
        content.parts = [types.Part.from_text(text=replacement), *kept]
        for value in stated:
            audit_event(
                "learning_answer_withheld",
                target=value.target_id,
                scenario_id=value.scenario_id,
                level=level,
                agent=callback_context.agent_name,
            )
        _count_withheld(len(stated))
        logger.info(
            "Withheld %d guarded value(s) from %s (scenario %s)",
            len(stated),
            callback_context.agent_name,
            first.scenario_id,
        )
        return None


def _count_withheld(n: int) -> None:
    """``orrery_learning_answers_withheld_total``, registered on first use."""
    global _WITHHELD
    if _WITHHELD is None:
        from prometheus_client import Counter

        _WITHHELD = Counter(
            "orrery_learning_answers_withheld_total",
            "Guarded values the answer guard kept out of a learner-facing reply",
        )
    _WITHHELD.inc(n)


_WITHHELD: Any = None
