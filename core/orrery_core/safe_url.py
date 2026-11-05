"""Parse a URL someone supplied, without repeating it if it is refused.

A server URL can hold a password, and refusals reach API callers, the audit log
and startup logs. The stdlib is the trap: ``urlsplit`` quotes the host part in some
errors, and reading ``.port`` quotes what it found, which for an unescaped password
(``https://u:pass/word@host``) is the password. Redaction can't catch these,
because to a parser they aren't credentials. So parse here, and never put the URL
in a message.
"""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit


def split_url(url: str, what: str = "URL") -> SplitResult:
    """``urlsplit(url)`` with a readable port, or a ``ValueError`` that omits *url*.

    *what* names the setting or field in the error.
    """
    try:
        parts = urlsplit(url)
        _ = parts.port  # lazy, and it raises on a port that isn't a number
    except ValueError:
        raise ValueError(
            f"{what}: the URL can't be parsed (a bad port, or an unescaped character)"
        ) from None
    return parts
