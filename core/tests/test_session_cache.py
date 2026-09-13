"""Unit tests for the bounded conversation-key → session-id cache."""

import pytest

from orrery_core.serving.session_cache import (
    DEFAULT_MAX_SESSION_MAPPINGS,
    BoundedSessionCache,
)


class TestBasicMapping:
    def test_get_returns_none_for_unknown_key(self):
        cache = BoundedSessionCache()
        assert cache.get(("alice", "thread-1")) is None

    def test_set_then_get(self):
        cache = BoundedSessionCache()
        cache.set(("alice", "thread-1"), "sess_a")
        assert cache.get(("alice", "thread-1")) == "sess_a"

    def test_set_overwrites(self):
        cache = BoundedSessionCache()
        cache.set(("alice", "thread-1"), "sess_a")
        cache.set(("alice", "thread-1"), "sess_b")
        assert cache.get(("alice", "thread-1")) == "sess_b"
        assert len(cache) == 1

    def test_discard(self):
        cache = BoundedSessionCache()
        cache.set(("alice", "thread-1"), "sess_a")
        cache.discard(("alice", "thread-1"))
        assert cache.get(("alice", "thread-1")) is None

    def test_discard_unknown_key_is_a_noop(self):
        cache = BoundedSessionCache()
        cache.discard(("nobody", "nothing"))  # must not raise

    def test_discard_where(self):
        cache = BoundedSessionCache()
        cache.set(("alice", "thread-1"), "sess_a")
        cache.set(("bob", "thread-1"), "sess_b")
        cache.set(("alice", "thread-2"), "sess_c")

        cache.discard_where(lambda key: key[1] == "thread-1")

        assert cache.get(("alice", "thread-1")) is None
        assert cache.get(("bob", "thread-1")) is None
        assert cache.get(("alice", "thread-2")) == "sess_c"


class TestCapacityBound:
    """The whole point: these caches live in processes that never restart."""

    def test_never_exceeds_capacity(self):
        cache = BoundedSessionCache(max_entries=50)
        for i in range(5_000):
            cache.set(("user", f"thread-{i}"), f"sess_{i}")
        assert len(cache) == 50

    def test_evicts_the_oldest_first(self):
        cache = BoundedSessionCache(max_entries=2)
        cache.set(("u", "a"), "sess_a")
        cache.set(("u", "b"), "sess_b")
        cache.set(("u", "c"), "sess_c")

        assert cache.get(("u", "a")) is None
        assert cache.get(("u", "b")) == "sess_b"
        assert cache.get(("u", "c")) == "sess_c"

    def test_a_read_keeps_an_entry_alive(self):
        """LRU, not FIFO — an actively used thread must not be evicted under a
        burst of new ones."""
        cache = BoundedSessionCache(max_entries=2)
        cache.set(("u", "busy"), "sess_busy")
        cache.set(("u", "idle"), "sess_idle")

        cache.get(("u", "busy"))  # touch
        cache.set(("u", "new"), "sess_new")

        assert cache.get(("u", "busy")) == "sess_busy"
        assert cache.get(("u", "idle")) is None

    def test_rewriting_an_entry_refreshes_it(self):
        cache = BoundedSessionCache(max_entries=2)
        cache.set(("u", "a"), "sess_a")
        cache.set(("u", "b"), "sess_b")
        cache.set(("u", "a"), "sess_a2")
        cache.set(("u", "c"), "sess_c")

        assert cache.get(("u", "a")) == "sess_a2"
        assert cache.get(("u", "b")) is None

    def test_capacity_of_one(self):
        cache = BoundedSessionCache(max_entries=1)
        cache.set(("u", "a"), "sess_a")
        cache.set(("u", "b"), "sess_b")
        assert len(cache) == 1
        assert cache.get(("u", "b")) == "sess_b"

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_a_capacity_that_can_hold_nothing(self, bad):
        with pytest.raises(ValueError, match="max_entries must be >= 1"):
            BoundedSessionCache(max_entries=bad)

    def test_default_capacity_is_generous(self):
        """Eviction silently restarts a live conversation, so the default must
        sit well above any plausible working set."""
        assert DEFAULT_MAX_SESSION_MAPPINGS >= 1_000
