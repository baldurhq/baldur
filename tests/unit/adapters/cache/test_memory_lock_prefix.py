"""Unit tests for ``InMemoryCacheAdapter.get_lock`` multi-instance isolation (#465).

Source: ``src/baldur/adapters/cache/memory_adapter.py``

Covers (#465 G3 / G5):

- ``InMemoryCacheAdapter.get_lock(name)`` resolves the registry key
  once via ``self._make_key(name)`` and passes it as ``full_key`` to
  ``InMemoryLock`` (D4/D6).
- Two ``InMemoryCacheAdapter`` instances with different ``key_prefix``
  values map the same user-facing lock name to distinct entries in the
  class-level ``InMemoryLock._locks`` registry — pre-#465 they
  collided on the raw name.
- ``InMemoryLock`` keys its registry entry on the verbatim ``full_key``;
  no in-class transformation, no ``f"lock:{name}"`` sentinel.
- ``clear_all_locks()`` semantics survive — the class-level registry
  is wiped regardless of which adapter created the entries.
- Per #465 D3, in-memory does NOT honor ``TestModeContext`` (Redis-only
  per #463 D9). The fix here is multi-instance prefix isolation only.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.4 Side effects (registry-key composition).
- §8.6 Data immutability (parallel adapters do not pollute each other).
- §8.10 Singleton/lifecycle (class-level registry cleanup).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.adapters.cache.memory_adapter import (
    InMemoryCacheAdapter,
    InMemoryLock,
)


@pytest.fixture(autouse=True)
def _clean_lock_registry():
    """Clear class-level lock registry between tests (avoids cross-test pollution)."""
    InMemoryLock.clear_all_locks()
    yield
    InMemoryLock.clear_all_locks()


# ---------------------------------------------------------------------------
# Multi-instance prefix isolation — the core G3 fix
# ---------------------------------------------------------------------------


class TestInMemoryLockMultiInstance:
    """Two adapters with different ``key_prefix`` lock the same name independently."""

    def test_distinct_prefixes_dont_collide_on_same_name(self):
        """Adapter A with ``a:`` and adapter B with ``b:`` both acquire ``foo``.

        Pre-#465 the class-level ``_locks`` was keyed on the raw name,
        so adapter B's ``acquire`` would block on adapter A's lock —
        a contract violation given that the adapters configured
        themselves to use distinct namespaces.
        """
        # Given
        adapter_a = InMemoryCacheAdapter(key_prefix="a:")
        adapter_b = InMemoryCacheAdapter(key_prefix="b:")

        # When
        lock_a = adapter_a.get_lock("foo")
        lock_b = adapter_b.get_lock("foo")
        assert lock_a.acquire(blocking=False) is True
        assert lock_b.acquire(blocking=False) is True

        # Then — both held simultaneously, registered under distinct keys
        assert "a:foo" in InMemoryLock._locks
        assert "b:foo" in InMemoryLock._locks
        assert lock_a.owned() is True
        assert lock_b.owned() is True

    def test_same_prefix_still_collides(self):
        """Two adapters with identical ``key_prefix`` correctly collide.

        Same prefix → same registry key → mutual exclusion preserved.
        """
        adapter_a = InMemoryCacheAdapter(key_prefix="shared:")
        adapter_b = InMemoryCacheAdapter(key_prefix="shared:")

        lock_a = adapter_a.get_lock("foo")
        lock_b = adapter_b.get_lock("foo")

        assert lock_a.acquire(blocking=False) is True
        assert lock_b.acquire(blocking=False) is False

    def test_clear_all_locks_wipes_both_prefixes(self):
        """``clear_all_locks()`` clears entries from all prefixes."""
        adapter_a = InMemoryCacheAdapter(key_prefix="a:")
        adapter_b = InMemoryCacheAdapter(key_prefix="b:")

        adapter_a.get_lock("foo").acquire(blocking=False)
        adapter_b.get_lock("foo").acquire(blocking=False)
        assert len(InMemoryLock._locks) == 2

        InMemoryLock.clear_all_locks()

        assert len(InMemoryLock._locks) == 0

    def test_flush_all_clears_locks(self):
        """``adapter.flush_all()`` also clears the class-level lock registry.

        ``InMemoryCacheAdapter.flush_all`` ends with
        ``InMemoryLock.clear_all_locks()`` for testing-cleanup
        symmetry — this contract survives the #465 patch.
        """
        adapter = InMemoryCacheAdapter(key_prefix="a:")
        adapter.get_lock("foo").acquire(blocking=False)
        assert len(InMemoryLock._locks) == 1

        adapter.flush_all()

        assert len(InMemoryLock._locks) == 0


# ---------------------------------------------------------------------------
# Lock-class contract: registry entry keyed on verbatim full_key
# ---------------------------------------------------------------------------


class TestInMemoryLockContract:
    """Direct ``InMemoryLock(full_key=...)`` registry-keying contract."""

    def test_registry_keyed_on_verbatim_full_key(self):
        """``acquire()`` registers the lock under the exact ``full_key``."""
        # Given
        full_key = "tenant-x:idempotency:lock:order:abc"

        # When
        lock = InMemoryLock(full_key=full_key, timeout=timedelta(seconds=5))
        lock.acquire(blocking=False)

        # Then
        assert full_key in InMemoryLock._locks
        # And no extra ``lock:`` segment was added by the lock class.
        assert f"lock:{full_key}" not in InMemoryLock._locks

    def test_distinct_full_keys_register_independently(self):
        """Same user-facing name + different prefixes → distinct registry entries."""
        lock_a = InMemoryLock(full_key="a:foo", timeout=timedelta(seconds=5))
        lock_b = InMemoryLock(full_key="b:foo", timeout=timedelta(seconds=5))

        assert lock_a.acquire(blocking=False) is True
        assert lock_b.acquire(blocking=False) is True

        assert set(InMemoryLock._locks.keys()) == {"a:foo", "b:foo"}

    def test_release_only_removes_matching_full_key(self):
        """Release on ``a:foo`` does not affect ``b:foo`` entry."""
        lock_a = InMemoryLock(full_key="a:foo", timeout=timedelta(seconds=5))
        lock_b = InMemoryLock(full_key="b:foo", timeout=timedelta(seconds=5))
        lock_a.acquire(blocking=False)
        lock_b.acquire(blocking=False)

        lock_a.release()

        assert "a:foo" not in InMemoryLock._locks
        assert "b:foo" in InMemoryLock._locks


# ---------------------------------------------------------------------------
# extend(): owner-fenced, must not self-deadlock on the non-reentrant
# registry lock (the latent bug exposed by the first cache-lock extend caller)
# ---------------------------------------------------------------------------


class TestInMemoryLockExtend:
    """``InMemoryLock.extend`` re-anchors the TTL only for the current owner."""

    def test_owner_extend_returns_true_without_hanging(self):
        """An acquired lock's owner extends the TTL and returns True.

        Regression: ``extend`` previously called ``owned()`` while holding the
        non-reentrant class registry lock — a guaranteed self-deadlock. This
        test would hang (caught by the suite ``--timeout``) before the fix.
        """
        lock = InMemoryLock(full_key="a:extend", timeout=timedelta(seconds=1))
        assert lock.acquire(blocking=False) is True

        before = lock._expires_at
        assert lock.extend(timedelta(seconds=60)) is True
        # TTL was re-anchored further out.
        assert lock._expires_at > before

    def test_non_owner_extend_returns_false(self):
        """A second instance (different owner) cannot extend a held lock."""
        owner = InMemoryLock(full_key="a:extend", timeout=timedelta(seconds=30))
        assert owner.acquire(blocking=False) is True

        other = InMemoryLock(full_key="a:extend", timeout=timedelta(seconds=30))
        # `other` never acquired — it is not the registered owner.
        assert other.extend(timedelta(seconds=60)) is False

    def test_extend_on_unheld_lock_returns_false(self):
        """Extending a lock with no registry entry returns False (no error)."""
        lock = InMemoryLock(full_key="a:never-acquired", timeout=timedelta(seconds=5))
        assert lock.extend(timedelta(seconds=60)) is False
