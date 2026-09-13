"""Maps a Slack thread participant to an ADK session.

A Slack thread is identified by (channel_id, thread_ts) — but a *session* is not.
ADK scopes every session by ``(app_name, user_id, session_id)``, so handing the
session created for the thread's first speaker to a second speaker does not join
them to that conversation: the lookup misses and ADK creates a fresh empty
session behind the same id. Threads are therefore per participant whether or not
the mapping admits it, and mapping ``(channel, thread_ts)`` alone only hid that —
while making it look as though history were shared.

The key is ``(channel_id, thread_ts, user_id)``: one session per person per
thread, which is what actually happens. Cross-participant continuity, if it is
ever wanted, needs a shared-history mechanism (a summary in ``app:``-scoped
state, or long-term memory), not a reused session id.
"""

from __future__ import annotations

from orrery_core.serving.session_cache import (
    DEFAULT_MAX_SESSION_MAPPINGS,
    BoundedSessionCache,
)


class SessionMap:
    """Capacity-bounded mapping from a (thread, participant) pair to a session ID.

    Both Slack entrypoints hold one of these as a module-level singleton for the
    life of the process, and ``remove`` is only reached on explicit session
    expiry — so the map gains an entry per participant per thread and, unbounded,
    never gives one back. It is a lookup shortcut over the durable session store
    (a miss just creates a fresh session, exactly as a restart does), which makes
    LRU eviction the cheap correct bound.

    Args:
        max_entries: Thread participants to keep mapped before dropping the
            least recently used.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_SESSION_MAPPINGS) -> None:
        self._map: BoundedSessionCache[tuple[str, str, str]] = BoundedSessionCache(max_entries)

    def get(self, channel: str, thread_ts: str, user_id: str) -> str | None:
        """Look up this participant's existing session ID for a thread."""
        return self._map.get((channel, thread_ts, user_id))

    def set(self, channel: str, thread_ts: str, user_id: str, session_id: str) -> None:
        """Store a session mapping for one participant in a thread."""
        self._map.set((channel, thread_ts, user_id), session_id)

    def remove(self, channel: str, thread_ts: str, user_id: str | None = None) -> None:
        """Forget mappings for a thread (e.g. on session expiry).

        Without *user_id*, every participant's mapping for the thread is dropped:
        expiry is a property of the thread, not of one speaker.
        """
        if user_id is not None:
            self._map.discard((channel, thread_ts, user_id))
            return
        self._map.discard_where(lambda key: key[0] == channel and key[1] == thread_ts)
