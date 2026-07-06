"""``InMemoryCacheAdapter`` lifecycle hygiene tests (484 D7).

Two additions from 484 D7:

1. ``cleanup_expired() -> int``
   Public, lock-acquiring entry point that wraps the private
   ``_cleanup_expired()`` (which assumes ``self._lock`` is already held).
   Per-process testing-only adapter, so the lock is a non-reentrant
   ``threading.Lock`` — external callers MUST use this public method, not
   the underscore helper, or they will deadlock the next public-method
   use on the same instance.

2. ``_instances: ClassVar[WeakSet]`` registry
   Every ``__init__`` adds ``self`` to the class-level WeakSet so
   ``CleanupService.cleanup_memory_cache_expired()`` can fan out to
   adapters that aren't registered with ``ProviderRegistry.cache``
   (ad-hoc fallbacks in ``decorators/idempotent.py``,
   ``services/security/*``, etc.). WeakSet membership: GC drops the
   instance automatically once the last strong reference is released.
   ``clear_all_instances()`` empties the registry for test fixtures.

Reference:
- ``484`` D7
- ``src/baldur/adapters/cache/memory_adapter.py``
"""

from __future__ import annotations

import gc
import threading
import time
from datetime import timedelta

import pytest

from baldur.adapters.cache.memory_adapter import (
    CacheEntry,
    InMemoryCacheAdapter,
)


@pytest.fixture
def isolated_registry():
    """Snapshot ``_instances`` before each test, restore after.

    The class-level WeakSet is shared global state; tests must not see
    each other's adapters via the registry.
    """
    saved = list(InMemoryCacheAdapter._instances)
    InMemoryCacheAdapter.clear_all_instances()
    try:
        yield
    finally:
        InMemoryCacheAdapter.clear_all_instances()
        for instance in saved:
            InMemoryCacheAdapter._instances.add(instance)


def _populate(adapter: InMemoryCacheAdapter, expired: int, fresh: int) -> None:
    """Populate an adapter with N expired entries (past ``expires_at``) plus M fresh ones.

    Bypasses the public ``set()`` for expired entries because ``set(ttl=...)``
    only accepts forward TTL values.
    """
    now = time.time()
    for i in range(expired):
        adapter._store[f"{adapter._key_prefix}stale_{i}"] = CacheEntry(
            value=f"v{i}",
            expires_at=now - 60,
        )
    for i in range(fresh):
        adapter.set(f"fresh_{i}", f"v{i}", ttl=timedelta(minutes=30))


# =============================================================================
# Behavior — cleanup_expired() return contract + lock acquisition
# =============================================================================


class TestMemoryAdapterCleanupExpiredBehavior:
    """484 D7: ``cleanup_expired()`` removes only expired entries and returns count."""

    def test_returns_zero_on_empty_store(self, isolated_registry):
        """Empty store → 0 removed."""
        adapter = InMemoryCacheAdapter(key_prefix="empty:")

        assert adapter.cleanup_expired() == 0

    def test_returns_zero_when_only_fresh_entries(self, isolated_registry):
        """All fresh → 0 removed; entries retained."""
        adapter = InMemoryCacheAdapter(key_prefix="fresh:")
        _populate(adapter, expired=0, fresh=5)

        assert adapter.cleanup_expired() == 0
        assert adapter.get_store_size() == 5

    def test_returns_count_of_expired_entries(self, isolated_registry):
        """Returned count equals number of removed expired entries."""
        adapter = InMemoryCacheAdapter(key_prefix="mix:")
        _populate(adapter, expired=4, fresh=2)

        removed = adapter.cleanup_expired()

        assert removed == 4
        assert adapter.get_store_size() == 2

    @pytest.mark.parametrize(
        ("expired", "fresh"),
        [(0, 0), (1, 0), (0, 1), (3, 7), (10, 0), (0, 10)],
        ids=[
            "empty",
            "single_expired",
            "single_fresh",
            "mixed",
            "all_expired",
            "all_fresh",
        ],
    )
    def test_parametrized_mix_returns_expired_count(
        self, isolated_registry, expired, fresh
    ):
        """Across mixes, count == expired and surviving size == fresh."""
        adapter = InMemoryCacheAdapter(key_prefix=f"p_{expired}_{fresh}:")
        _populate(adapter, expired=expired, fresh=fresh)

        removed = adapter.cleanup_expired()

        assert removed == expired
        assert adapter.get_store_size() == fresh

    def test_idempotent_second_call_returns_zero(self, isolated_registry):
        """Re-running cleanup on a swept adapter is a no-op."""
        adapter = InMemoryCacheAdapter(key_prefix="idem:")
        _populate(adapter, expired=3, fresh=2)

        first = adapter.cleanup_expired()
        second = adapter.cleanup_expired()

        assert first == 3
        assert second == 0

    def test_acquires_self_lock_around_cleanup(self, isolated_registry):
        """``cleanup_expired()`` must hold ``self._lock`` while sweeping.

        Substituting ``self._lock`` with an instrumented lock lets us
        verify the cleanup runs inside the held-lock region (the private
        ``_cleanup_expired()`` is unsafe to call without the lock held).
        """
        adapter = InMemoryCacheAdapter(key_prefix="lock:")
        _populate(adapter, expired=2, fresh=1)

        original_lock = adapter._lock
        held_during_cleanup = {"value": False}

        class InstrumentedLock:
            def __enter__(self_inner):
                original_lock.acquire()
                held_during_cleanup["entered"] = True
                return self_inner

            def __exit__(self_inner, exc_type, exc, tb):
                # Confirm the underlying lock is still held when private
                # cleanup completes — i.e., we never released it mid-sweep.
                held_during_cleanup["value"] = original_lock.locked()
                original_lock.release()
                return False

            def acquire(self_inner, *a, **kw):
                return original_lock.acquire(*a, **kw)

            def release(self_inner):
                original_lock.release()

            def locked(self_inner):
                return original_lock.locked()

        adapter._lock = InstrumentedLock()

        removed = adapter.cleanup_expired()

        assert removed == 2
        assert held_during_cleanup["entered"] is True
        assert held_during_cleanup["value"] is True

    def test_cleanup_does_not_deadlock_when_followed_by_public_op(
        self, isolated_registry
    ):
        """After ``cleanup_expired()``, the next public op must not deadlock.

        This is the regression contract: the internal ``_cleanup_expired()``
        is unlocked, so calling it externally would leave ``self._lock``
        held. The public ``cleanup_expired()`` releases properly, so a
        follow-up ``get()`` must complete promptly.
        """
        adapter = InMemoryCacheAdapter(key_prefix="dlk:")
        _populate(adapter, expired=1, fresh=1)
        adapter.cleanup_expired()

        completed = threading.Event()

        def follow_up():
            adapter.set("after", "value")
            adapter.get("after")
            completed.set()

        worker = threading.Thread(target=follow_up)
        worker.start()
        worker.join(timeout=2.0)

        assert completed.is_set(), "cleanup_expired() left self._lock held"


# =============================================================================
# Behavior — _instances WeakSet registry
# =============================================================================


class TestMemoryAdapterInstanceRegistryBehavior:
    """484 D7: ``_instances`` ``WeakSet`` lifecycle (register / GC / clear)."""

    def test_init_registers_instance_in_weakset(self, isolated_registry):
        """Each ``__init__`` adds the new adapter to ``_instances``."""
        a = InMemoryCacheAdapter(key_prefix="r1:")

        assert a in InMemoryCacheAdapter._instances
        assert len(InMemoryCacheAdapter._instances) == 1

    def test_multiple_init_registers_each_distinct_instance(self, isolated_registry):
        """N constructors → N distinct registry entries."""
        a = InMemoryCacheAdapter(key_prefix="r1:")
        b = InMemoryCacheAdapter(key_prefix="r2:")
        c = InMemoryCacheAdapter(key_prefix="r3:")

        assert {a, b, c} <= set(InMemoryCacheAdapter._instances)
        assert len(InMemoryCacheAdapter._instances) == 3

    def test_gc_removes_instance_when_last_reference_drops(self, isolated_registry):
        """``WeakSet`` semantics: GC reclaims entries when refcount hits 0."""
        InMemoryCacheAdapter(key_prefix="gc:")
        # No name binding kept → GC eligible immediately.

        gc.collect()

        # Entry should be gone after collection.
        assert len(InMemoryCacheAdapter._instances) == 0

    def test_strong_reference_keeps_entry_alive_through_gc(self, isolated_registry):
        """A live strong reference keeps the WeakSet entry across gc.collect()."""
        a = InMemoryCacheAdapter(key_prefix="alive:")
        gc.collect()

        assert a in InMemoryCacheAdapter._instances

    def test_clear_all_instances_empties_registry(self, isolated_registry):
        """``clear_all_instances()`` removes all entries (test fixture helper)."""
        InMemoryCacheAdapter(key_prefix="c1:")
        InMemoryCacheAdapter(key_prefix="c2:")
        # Hold strong refs in a list so GC can't sneak them out from under us.
        keep = [
            InMemoryCacheAdapter(key_prefix="c3:"),
            InMemoryCacheAdapter(key_prefix="c4:"),
        ]
        assert len(InMemoryCacheAdapter._instances) >= 2

        InMemoryCacheAdapter.clear_all_instances()

        assert len(InMemoryCacheAdapter._instances) == 0
        # The strong refs still exist; the registry is just empty.
        assert all(isinstance(a, InMemoryCacheAdapter) for a in keep)

    def test_clear_all_instances_does_not_break_subsequent_init(
        self, isolated_registry
    ):
        """After ``clear_all_instances()``, new adapters re-populate the registry."""
        InMemoryCacheAdapter.clear_all_instances()
        new_adapter = InMemoryCacheAdapter(key_prefix="post_clear:")

        assert new_adapter in InMemoryCacheAdapter._instances
        assert len(InMemoryCacheAdapter._instances) == 1
