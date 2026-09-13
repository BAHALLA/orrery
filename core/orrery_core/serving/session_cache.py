"""Bounded conversation-key → session-id cache shared by the chat surfaces.

Slack threads, Chat spaces and the CLI all identify a conversation by something
that is *not* an ADK session id, so each keeps an in-process mapping from its
own key to the session it created. The sessions themselves live in the session
store; this mapping is only a lookup shortcut, and both surfaces already treat
losing it as survivable — it is rebuilt from scratch on every restart.

What it must not do is grow forever. These maps live in long-running processes
(the Slack and Google Chat bots hold one as a module-level singleton) and gain
an entry per participant per thread, with no removal on any production path:
the ``forget``/``remove`` methods exist for an explicit "new conversation", not
for eviction. Capacity-bounding them costs the least-recently-used thread the
same continuity a restart already costs it, and bounds the process instead.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable

#: Active (participant, thread) pairs kept before the oldest is dropped. An
#: entry is a few short strings, so this is single-digit MB — chosen to sit far
#: above any plausible working set of live threads rather than to save memory,
#: since evicting a thread someone is still talking in silently restarts it.
DEFAULT_MAX_SESSION_MAPPINGS = 10_000


class BoundedSessionCache[K: Hashable]:
    """Fixed-capacity LRU mapping a conversation key to an ADK session id.

    Not thread-safe: the surfaces that use it drive it from a single asyncio
    event loop, and every method here runs to completion without awaiting.

    Args:
        max_entries: Entries to retain. The least recently *used* entry (read
            or written) is dropped once the cache is full.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_SESSION_MAPPINGS) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._max_entries = max_entries
        self._entries: OrderedDict[K, str] = OrderedDict()

    def get(self, key: K) -> str | None:
        """Return the session id for *key*, marking it most recently used."""
        session_id = self._entries.get(key)
        if session_id is not None:
            self._entries.move_to_end(key)
        return session_id

    def set(self, key: K, session_id: str) -> None:
        """Store *session_id* under *key*, evicting the oldest entry if full."""
        self._entries[key] = session_id
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def discard(self, key: K) -> None:
        """Drop *key* if present."""
        self._entries.pop(key, None)

    def discard_where(self, predicate: Callable[[K], bool]) -> None:
        """Drop every key matching *predicate* (e.g. all participants in a thread)."""
        for key in [k for k in self._entries if predicate(k)]:
            del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)
