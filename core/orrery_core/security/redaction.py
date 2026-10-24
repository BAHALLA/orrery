"""Secret-shape patterns and the text redactor, in one place.

Two subsystems scrub the same kinds of secret out of free-form text: the
``PIIRedactionPlugin`` on the way out of a tool, and ``SecureMemoryService`` on
the way into long-term memory. They used to carry separate pattern lists, and the
memory list was the shorter one — it matched ``password=…`` and PEM blocks but
none of the bare provider tokens. A ``ghp_…`` pasted into chat was therefore
stored verbatim, and any later ``load_memory`` recall handed it back.

One list, one redactor, both callers. A pattern added for either path now covers
both, which is the only arrangement where "we redact provider tokens" stays true.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Secret shapes inside free-form string values (log lines, config dumps, ...).
#
# Each entry pairs a pattern with the case-sensitive literals it cannot match
# without. Every pattern otherwise scans the full text, and on the multi-MiB log
# payloads this platform routinely handles that is the dominant cost of the
# whole after-tool chain — while the provider-token shapes almost never fire.
# ``"AKIA" in text`` is a memchr-style scan, far cheaper than running the regex
# engine over the same bytes, so an absent literal skips its pattern outright.
# Measured 2x faster on clean log text with byte-identical output.
#
# A literal here is a **correctness claim**: it must appear in every string the
# pattern can match. Anchor it on the fixed prefix, never on a character class.
# An empty tuple means "always run" — the right choice for the case-insensitive
# key=value pattern, whose triggers would need the text lowercased first (a full
# copy, which costs more than it saves).
SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    # key=value / key: value credential pairs
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_\-]?key|apikey|"
            r"credential|authorization|bearer)\b\s*[:=]\s*\S+"
        ),
        (),
    ),
    # PEM private-key blocks
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        ("-----BEGIN",),
    ),
    # Provider token prefixes: AWS access key, GitHub, Slack, OpenAI-style
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), ("AKIA",)),
    # Credentials in a URL: postgres://user:pass@db, redis://:pass@cache. Only
    # the userinfo goes, so the scheme and host stay readable. Greedy up to the
    # *last* "@" before the path, so a password holding "@" goes whole. Quotes,
    # commas, "?" and "#" end it: otherwise "http://svc:8080" followed by any
    # later "@" in a compact JSON log line would be read as one credential.
    (re.compile(r"(?<=://)[^\s/@:?#\"'<>,]*:[^\s/?#\"'<>,]+(?=@)"), ("://",)),
    # Google API keys (Gemini, Maps, Firebase): "AIza" + 35 characters.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), ("AIza",)),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), ("xox",)),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b"), ("sk-",)),
    # JWT: three dot-separated base64url segments
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        ("eyJ",),
    ),
)

IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
REDACTED_IP = "[REDACTED_IP]"

#: Email addresses. Off by default (an SRE answer names owners and on-call
#: people), on for text that arrives from a third party: a remote A2A agent's
#: reply (ADR-008) and Studio trace content (AEP-031).
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
REDACTED_EMAIL = "[REDACTED_EMAIL]"


def redact_text(
    text: str, *, redact_ips: bool = False, redact_emails: bool = False
) -> tuple[str, int]:
    """Return *text* with secret shapes replaced, plus the replacement count."""
    count = 0
    for pattern, triggers in SECRET_VALUE_PATTERNS:
        # Skip the regex entirely when a literal the pattern requires is absent.
        if triggers and not any(trigger in text for trigger in triggers):
            continue
        text, n = pattern.subn(REDACTED, text)
        count += n
    if redact_ips:
        text, n = IPV4_PATTERN.subn(REDACTED_IP, text)
        count += n
    if redact_emails and "@" in text:
        text, n = EMAIL_PATTERN.subn(REDACTED_EMAIL, text)
        count += n
    return text, count
