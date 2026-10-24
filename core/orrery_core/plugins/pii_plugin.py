"""PII / secret redaction for tool outputs (AEP-013).

``PIIRedactionPlugin`` scrubs credentials from every tool result before the
model, the session store, or any after-tool observer sees them. Two layers:

1. **Key-based**: dict entries whose key names a credential (``password``,
   ``access_token``, ``api_key``, ...) have their value replaced outright —
   the most reliable signal, immune to value-format drift.
2. **Pattern-based**: string values are scanned for well-known secret shapes
   (``password=...`` pairs, PEM blocks, AWS/GitHub/Slack/OpenAI token
   prefixes, JWTs) and matches are replaced in place.

Redaction **mutates the result in place and returns None** — deliberately.
ADK's after-tool chain early-exits on the first non-None return, so returning
a redacted copy would silence every observer registered later (audit outcome,
activity, metrics, output cap). Mutating the shared dict keeps the chain
intact while ensuring all downstream observers record the redacted values —
which is the point: the audit log must not carry secrets either. For the same
reason the plugin is registered *before* ``AuditPlugin`` in
``default_plugins()``.

Infrastructure identifiers (IP addresses) are **not** redacted by default —
an SRE agent that cannot see pod IPs or broker addresses cannot diagnose
anything. Opt in with ``redact_ips=True`` (``ORRERY_REDACT_IPS`` via
``default_plugins``) for compliance-bound deployments.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..payload import OFFLOAD_THRESHOLD_CHARS, mutable_attributes, text_volume
from ..security.redaction import (
    IPV4_PATTERN as IPV4_PATTERN,
)
from ..security.redaction import (
    REDACTED as REDACTED,
)
from ..security.redaction import (
    REDACTED_IP as REDACTED_IP,
)
from ..security.redaction import (
    SECRET_VALUE_PATTERNS as SECRET_VALUE_PATTERNS,
)
from ..security.redaction import redact_text

logger = logging.getLogger("orrery.pii")

# Redaction moves to a worker thread above OFFLOAD_THRESHOLD_CHARS. Scanning
# costs roughly 60 ms per MiB here (several regex passes over the same text), so a
# 20 MiB `get_pod_logs` result measured ~1.3 s of blocking. The threshold lives in
# `orrery_core.payload` because the safety screen makes the same call about the
# same payload, and the two must not drift apart.

# Dict keys that carry credentials. Keys are normalized to snake_case first
# (camelCase/PascalCase boundaries become underscores), then matched against
# the end of the key — so "access_token", "accessToken", and "DbPassword" all
# hit while "token_count" does not. Matching on the normalized form (rather
# than a case-insensitive camel lookaround in the pattern) keeps the
# allowlist meaningful: "nextPageToken" normalizes to the allowlisted
# "next_page_token" instead of being redacted as a token.
# "token" is singular-only: plural endings ("total_tokens", "maxTokens") are
# LLM usage counts, not credentials.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|[_\-.])"
    r"(?:(?:password|passwd|pwd|secret|api[_\-]?key|apikey|authorization|"
    r"credential|private[_\-]?key|access[_\-]?key|session[_\-]?key|"
    r"client[_\-]?secret|signing[_\-]?key|cert[_\-]?key)s?|token)$"
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _normalize_key(key: str) -> str:
    """``dbPassword`` → ``db_password``; ``AccessToken`` → ``access_token``."""
    return _CAMEL_BOUNDARY.sub("_", key).lower()


# Keys that end in a sensitive word but are opaque cursors, not credentials.
# Compared against the *normalized* key, so camelCase variants are covered.
KEY_ALLOWLIST = frozenset({"next_page_token", "page_token", "continue_token", "resume_token"})

# The pattern set and the text redactor live in ``security/redaction.py`` — shared
# with ``SecureMemoryService``, which scrubs the same secret shapes on the way into
# long-term memory. They stay importable from here (the import above rebinds them
# as module attributes) because that is this module's established surface.

_MAX_DEPTH = 32


def _redact_text(text: str, *, redact_ips: bool, redact_emails: bool = False) -> tuple[str, int]:
    """Return *text* with secret shapes replaced, plus the replacement count."""
    return redact_text(text, redact_ips=redact_ips, redact_emails=redact_emails)


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return normalized not in KEY_ALLOWLIST and bool(SENSITIVE_KEY_PATTERN.search(normalized))


def redact_structure(
    obj: Any, *, redact_ips: bool = False, redact_emails: bool = False, _depth: int = 0
) -> int:
    """Redact secrets in *obj* **in place**; return replacements made.

    Walks dicts, lists, and the attributes of ordinary objects — the last of those
    is what reaches a Pydantic tool result such as ADK's ``LoadMemoryResponse``,
    which a dict/list-only walk skipped entirely.

    Strings nested in tuples/sets are left alone (rebuilding an immutable container
    would change object identity). A *bare* string result cannot be mutated at all;
    :class:`PIIRedactionPlugin` handles that by returning a replacement.
    """
    if _depth > _MAX_DEPTH:
        return 0
    count = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and _is_sensitive_key(key) and value not in (None, ""):
                obj[key] = REDACTED
                count += 1
            elif isinstance(value, str):
                redacted, n = _redact_text(
                    value, redact_ips=redact_ips, redact_emails=redact_emails
                )
                if n:
                    obj[key] = redacted
                    count += n
            else:
                count += redact_structure(
                    value, redact_ips=redact_ips, redact_emails=redact_emails, _depth=_depth + 1
                )
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                redacted, n = _redact_text(item, redact_ips=redact_ips, redact_emails=redact_emails)
                if n:
                    obj[i] = redacted
                    count += n
            else:
                count += redact_structure(
                    item, redact_ips=redact_ips, redact_emails=redact_emails, _depth=_depth + 1
                )
    elif (attributes := mutable_attributes(obj)) is not None:
        for name, value in list(attributes.items()):
            if _is_sensitive_key(name) and value not in (None, ""):
                setattr(obj, name, REDACTED)
                count += 1
            elif isinstance(value, str):
                redacted, n = _redact_text(
                    value, redact_ips=redact_ips, redact_emails=redact_emails
                )
                if n:
                    setattr(obj, name, redacted)
                    count += n
            else:
                count += redact_structure(
                    value, redact_ips=redact_ips, redact_emails=redact_emails, _depth=_depth + 1
                )
    return count


class PIIRedactionPlugin(BasePlugin):
    """Scrubs credentials from tool results before anything downstream sees them."""

    def __init__(self, *, redact_ips: bool = False) -> None:
        super().__init__(name="pii_redaction")
        self._redact_ips = redact_ips

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> Any:
        # A bare string cannot be mutated, so the only way to scrub one is to
        # return the replacement — see _redact_immutable_result.
        # A tool that relays a third party's text (a remote A2A agent) asks for
        # contact details to go too: a person's address is not the evidence.
        emails = getattr(tool, "redact_contact_details", False) is True
        if isinstance(result, (str, bytes)):
            return _redact_immutable_result(result, tool=tool, redact_ips=self._redact_ips)

        # In-place + return None: a non-None return would early-exit ADK's
        # after-tool chain and skip every observer registered after this one.
        # Mutating in a worker thread is safe for the same reason it is safe
        # here — nothing else touches the result until this callback returns.
        if text_volume(result, OFFLOAD_THRESHOLD_CHARS) >= OFFLOAD_THRESHOLD_CHARS:
            count = await asyncio.to_thread(
                redact_structure, result, redact_ips=self._redact_ips, redact_emails=emails
            )
        else:
            count = redact_structure(result, redact_ips=self._redact_ips, redact_emails=emails)
        if count:
            logger.warning("redacted %d secret value(s) from '%s' result", count, tool.name)
        return None


def _redact_immutable_result(result: Any, *, tool: BaseTool, redact_ips: bool) -> Any:
    """Scrub a scalar tool result by *returning* a redacted copy, or ``None``.

    Every shipped tool returns a dict, but ADK does not require it: a tool may
    return a bare string, and nothing normalizes it to a dict until
    ``__build_response_event`` — which runs *after* this chain. Such a result used
    to pass through untouched, carrying whatever it held into the model context,
    the audit log, and the session store.

    Returning a value early-exits ADK's after-tool chain, so the observers
    registered after this plugin (audit outcome, activity, metrics, the size cap)
    do not see this call. That is the unavoidable trade for a value that cannot be
    mutated, and it is the right way round: losing one audit *outcome* line beats
    writing a live credential into it. It is logged at warning level so the gap is
    never silent, and only happens when there was something to redact — a clean
    string returns ``None`` and the chain proceeds normally.

    The shape the model sees is unchanged either way: ADK wraps a non-dict result
    as ``{"result": ...}`` regardless of whether this plugin replaced it.
    """
    text = result.decode("utf-8", "replace") if isinstance(result, bytes) else result
    redacted, count = redact_text(text, redact_ips=redact_ips)
    if not count:
        return None
    logger.warning(
        "redacted %d secret value(s) from '%s' result, which was a bare %s. "
        "Returning a replacement short-circuits the remaining after-tool observers "
        "(audit outcome, activity, metrics, output cap) for this call — return a dict "
        "from the tool to keep them.",
        count,
        tool.name,
        type(result).__name__,
    )
    return redacted
