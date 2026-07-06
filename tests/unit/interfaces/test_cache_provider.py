"""
Unit tests for CacheProviderInterface.

Tests the abstract interface contract and in-memory implementation.
"""

import inspect
import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import (
    InMemoryCacheAdapter,
    InMemoryLock,  # Renamed from InMemoryDistributedLock
)
from baldur.interfaces.cache_provider import (
    CacheProviderInterface,
    DistributedLock,
)
from tests.factories.concurrency_helpers import make_observable_singleflight

# Doc 594 D11 observability contract (structlog WARNING events)
SINGLEFLIGHT_BACKEND_FAILED_EVENT = "cache_provider.singleflight_backend_failed"
SINGLEFLIGHT_WAIT_TIMEOUT_EVENT = "cache_provider.singleflight_wait_timeout"


class TestDistributedLockInterface:
    """Tests for DistributedLock abstract interface."""

    def test_abstract_methods_required(self):
        """Test that all abstract methods must be implemented."""
        with pytest.raises(TypeError):
            DistributedLock()


class TestInMemoryCacheAdapter:
    """Tests for InMemoryCacheAdapter implementation."""

    @pytest.fixture
    def cache(self):
        """Create an in-memory cache adapter."""
        return InMemoryCacheAdapter(key_prefix="test:")

    def test_provider_name(self, cache: InMemoryCacheAdapter):
        """Test provider name."""
        assert cache.provider_name == "memory"

    def test_implements_interface(self, cache: InMemoryCacheAdapter):
        """Test that adapter implements CacheProviderInterface."""
        assert isinstance(cache, CacheProviderInterface)

    # =========================================================================
    # Basic Operations Tests
    # =========================================================================

    def test_set_and_get(self, cache: InMemoryCacheAdapter):
        """Test basic set and get operations."""
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_get_nonexistent_key(self, cache: InMemoryCacheAdapter):
        """Test getting a nonexistent key returns None."""
        assert cache.get("nonexistent") is None

    def test_set_with_various_types(self, cache: InMemoryCacheAdapter):
        """Test setting various data types."""
        # String
        cache.set("string_key", "hello")
        assert cache.get("string_key") == "hello"

        # Integer
        cache.set("int_key", 42)
        assert cache.get("int_key") == 42

        # Float
        cache.set("float_key", 3.14)
        assert cache.get("float_key") == 3.14

        # List
        cache.set("list_key", [1, 2, 3])
        assert cache.get("list_key") == [1, 2, 3]

        # Dict
        cache.set("dict_key", {"a": 1, "b": 2})
        assert cache.get("dict_key") == {"a": 1, "b": 2}

        # None value
        cache.set("none_key", None)
        assert cache.get("none_key") is None

    def test_set_overwrites_existing(self, cache: InMemoryCacheAdapter):
        """Test that set overwrites existing value."""
        cache.set("key", "original")
        cache.set("key", "updated")
        assert cache.get("key") == "updated"

    def test_delete_existing_key(self, cache: InMemoryCacheAdapter):
        """Test deleting an existing key."""
        cache.set("key", "value")
        result = cache.delete("key")
        assert result is True
        assert cache.get("key") is None

    def test_delete_nonexistent_key(self, cache: InMemoryCacheAdapter):
        """Test deleting a nonexistent key."""
        result = cache.delete("nonexistent")
        assert result is False

    def test_exists_key(self, cache: InMemoryCacheAdapter):
        """Test exists check for keys."""
        cache.set("key", "value")
        assert cache.exists("key") is True
        assert cache.exists("nonexistent") is False

    # =========================================================================
    # TTL Tests
    # =========================================================================

    def test_set_with_ttl(self, cache: InMemoryCacheAdapter):
        """Test setting value with TTL."""
        cache.set("key", "value", ttl=timedelta(seconds=0.1))
        assert cache.get("key") == "value"
        time.sleep(0.15)
        assert cache.get("key") is None

    def test_ttl_returns_remaining_seconds(self, cache: InMemoryCacheAdapter):
        """Test TTL returns remaining seconds."""
        cache.set("key", "value", ttl=timedelta(seconds=10))
        remaining = cache.ttl("key")
        assert remaining is not None
        assert 8 <= remaining <= 10

    def test_ttl_returns_none_for_no_expiry(self, cache: InMemoryCacheAdapter):
        """Test TTL returns None for keys without expiry."""
        cache.set("key", "value")
        assert cache.ttl("key") is None

    def test_ttl_returns_none_for_nonexistent(self, cache: InMemoryCacheAdapter):
        """Test TTL returns None for nonexistent keys."""
        result = cache.ttl("nonexistent")
        # Implementation returns -2 or None for missing keys
        assert result is None or result == -2

    def test_expire_sets_ttl_on_existing_key(self, cache: InMemoryCacheAdapter):
        """Test expire sets TTL on existing key."""
        cache.set("key", "value")
        result = cache.expire("key", timedelta(seconds=5))
        assert result is True
        remaining = cache.ttl("key")
        assert remaining is not None
        assert 3 <= remaining <= 5

    def test_expire_nonexistent_key(self, cache: InMemoryCacheAdapter):
        """Test expire on nonexistent key returns False."""
        result = cache.expire("nonexistent", timedelta(seconds=5))
        assert result is False

    def test_expired_key_not_exists(self, cache: InMemoryCacheAdapter):
        """Test that expired key does not exist."""
        cache.set("key", "value", ttl=timedelta(milliseconds=100))
        time.sleep(0.2)
        assert cache.exists("key") is False

    # =========================================================================
    # Atomic Operations Tests
    # =========================================================================

    def test_incr_new_key(self, cache: InMemoryCacheAdapter):
        """Test incrementing a new key starts from 0."""
        result = cache.incr("counter")
        assert result == 1

    def test_incr_existing_key(self, cache: InMemoryCacheAdapter):
        """Test incrementing an existing key."""
        cache.set("counter", 5)
        result = cache.incr("counter")
        assert result == 6

    def test_incr_by_amount(self, cache: InMemoryCacheAdapter):
        """Test incrementing by custom amount."""
        cache.set("counter", 10)
        result = cache.incr("counter", amount=5)
        assert result == 15

    def test_decr_existing_key(self, cache: InMemoryCacheAdapter):
        """Test decrementing an existing key."""
        cache.set("counter", 10)
        result = cache.decr("counter")
        assert result == 9

    def test_decr_by_amount(self, cache: InMemoryCacheAdapter):
        """Test decrementing by custom amount."""
        cache.set("counter", 20)
        result = cache.decr("counter", amount=5)
        assert result == 15

    def test_incr_thread_safety(self, cache: InMemoryCacheAdapter):
        """Test that increment is thread-safe."""
        cache.set("counter", 0)

        def increment():
            for _ in range(100):
                cache.incr("counter")

        threads = [threading.Thread(target=increment) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cache.get("counter") == 1000

    # =========================================================================
    # Distributed Locking Tests
    # =========================================================================

    def test_get_lock_returns_lock(self, cache: InMemoryCacheAdapter):
        """Test get_lock returns a lock instance."""
        lock = cache.get_lock("test_lock")
        assert lock is not None
        assert isinstance(lock, DistributedLock)

    def test_lock_acquire_and_release(self, cache: InMemoryCacheAdapter):
        """Test acquiring and releasing a lock."""
        lock = cache.get_lock("test_lock")
        assert lock.acquire() is True
        assert lock.locked() is True
        lock.release()
        assert lock.locked() is False

    def test_lock_context_manager(self, cache: InMemoryCacheAdapter):
        """Test lock as context manager."""
        lock = cache.get_lock("test_lock")
        with lock:
            assert lock.locked() is True
        assert lock.locked() is False

    def test_lock_prevents_concurrent_access(self, cache: InMemoryCacheAdapter):
        """Test that lock prevents concurrent access."""
        lock = cache.get_lock("test_lock")
        results = []

        def task(task_id):
            with lock:
                results.append(f"start_{task_id}")
                time.sleep(0.05)
                results.append(f"end_{task_id}")

        threads = [threading.Thread(target=task, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify sequential execution (start_i must be followed by end_i)
        for i in range(3):
            start_idx = results.index(f"start_{i}")
            end_idx = results.index(f"end_{i}")
            assert end_idx == start_idx + 1

    def test_lock_acquire_nonblocking(self, cache: InMemoryCacheAdapter):
        """Test non-blocking lock acquire."""
        lock1 = cache.get_lock("test_lock")
        lock2 = cache.get_lock("test_lock")

        assert lock1.acquire(blocking=False) is True
        assert lock2.acquire(blocking=False) is False
        lock1.release()

    def test_lock_acquire_with_timeout(self, cache: InMemoryCacheAdapter):
        """Test lock acquire with timeout."""
        lock1 = cache.get_lock("test_lock")
        lock2 = cache.get_lock("test_lock")

        lock1.acquire()
        start_time = time.time()
        result = lock2.acquire(blocking=True, timeout=0.5)
        elapsed = time.time() - start_time

        assert result is False
        assert elapsed >= 0.4  # Should have waited near timeout
        lock1.release()

    # =========================================================================
    # Bulk Operations Tests
    # =========================================================================

    def test_mget_multiple_keys(self, cache: InMemoryCacheAdapter):
        """Test getting multiple keys at once."""
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        result = cache.mget(["key1", "key2", "key3"])
        assert result == {"key1": "value1", "key2": "value2", "key3": "value3"}

    def test_mget_with_missing_keys(self, cache: InMemoryCacheAdapter):
        """Test mget excludes missing keys."""
        cache.set("key1", "value1")
        result = cache.mget(["key1", "nonexistent"])
        assert result == {"key1": "value1"}

    def test_mget_empty_list(self, cache: InMemoryCacheAdapter):
        """Test mget with empty list."""
        result = cache.mget([])
        assert result == {}

    def test_mset_multiple_keys(self, cache: InMemoryCacheAdapter):
        """Test setting multiple keys at once."""
        result = cache.mset({"key1": "value1", "key2": "value2"})
        assert result is True
        assert cache.get("key1") == "value1"
        assert cache.get("key2") == "value2"

    def test_mset_with_ttl(self, cache: InMemoryCacheAdapter):
        """Test mset with TTL."""
        cache.mset({"key1": "value1", "key2": "value2"}, ttl=timedelta(seconds=0.1))
        assert cache.get("key1") == "value1"
        time.sleep(0.15)
        assert cache.get("key1") is None
        assert cache.get("key2") is None

    # =========================================================================
    # Health Check Tests
    # =========================================================================

    def test_health_check_healthy(self, cache: InMemoryCacheAdapter):
        """Test health check returns True for healthy cache."""
        assert cache.health_check() is True

    def test_flush_all(self, cache: InMemoryCacheAdapter):
        """Test flush_all clears all keys."""
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        result = cache.flush_all()
        assert result is True
        assert cache.get("key1") is None
        assert cache.get("key2") is None

    # =========================================================================
    # List Operations Tests (push_limit / list_range)
    # =========================================================================

    def test_push_limit_to_empty_key_creates_list(self, cache: InMemoryCacheAdapter):
        """push_limit on non-existent key creates a new single-element list."""
        result = cache.push_limit("new_list", "item1", max_len=10)
        assert result == 1
        assert cache.list_range("new_list", 0, -1) == ["item1"]

    def test_push_limit_appends_to_existing_list(self, cache: InMemoryCacheAdapter):
        """push_limit on existing list appends and returns pre-trim length."""
        cache.push_limit("mylist", "a", max_len=10)
        cache.push_limit("mylist", "b", max_len=10)
        result = cache.push_limit("mylist", "c", max_len=10)
        assert result == 3
        assert cache.list_range("mylist", 0, -1) == ["a", "b", "c"]

    def test_push_limit_trims_oldest_when_exceeds_max_len(
        self, cache: InMemoryCacheAdapter
    ):
        """push_limit drops oldest entries when list exceeds max_len."""
        for i in range(5):
            cache.push_limit("bounded", i, max_len=3)

        # Pre-trim length on last push: 4 (was 3, pushed to 4 before trim → kept 3)
        items = cache.list_range("bounded", 0, -1)
        assert items == [2, 3, 4]

    def test_push_limit_returns_pre_trim_length(self, cache: InMemoryCacheAdapter):
        """push_limit returns length after push but before trim (> max_len detects trim)."""
        for i in range(3):
            cache.push_limit("pl", i, max_len=3)

        # 4th push: pre-trim = 4, post-trim = 3
        result = cache.push_limit("pl", "overflow", max_len=3)
        assert result == 4  # pre-trim length > max_len → trim occurred
        assert len(cache.list_range("pl", 0, -1)) == 3

    def test_push_limit_max_len_one(self, cache: InMemoryCacheAdapter):
        """push_limit with max_len=1 always keeps only the latest item."""
        cache.push_limit("single", "first", max_len=1)
        cache.push_limit("single", "second", max_len=1)
        assert cache.list_range("single", 0, -1) == ["second"]

    def test_push_limit_with_ttl_sets_expiration(self, cache: InMemoryCacheAdapter):
        """push_limit with TTL sets key expiration."""
        cache.push_limit("ttl_list", "v", max_len=10, ttl=timedelta(seconds=10))
        remaining = cache.ttl("ttl_list")
        assert remaining is not None
        assert 8 <= remaining <= 10

    def test_push_limit_without_ttl_no_expiration(self, cache: InMemoryCacheAdapter):
        """push_limit without TTL creates key with no expiration."""
        cache.push_limit("no_ttl", "v", max_len=10)
        assert cache.ttl("no_ttl") is None

    def test_push_limit_on_expired_key_creates_new_list(
        self, cache: InMemoryCacheAdapter
    ):
        """push_limit on expired key starts fresh list."""
        cache.push_limit("exp", "old", max_len=10, ttl=timedelta(seconds=0.05))
        time.sleep(0.1)
        result = cache.push_limit("exp", "new", max_len=10)
        assert result == 1
        assert cache.list_range("exp", 0, -1) == ["new"]

    def test_push_limit_thread_safety(self, cache: InMemoryCacheAdapter):
        """Concurrent push_limit calls do not lose entries."""
        errors = []

        def pusher(thread_id):
            try:
                for i in range(50):
                    cache.push_limit("ts_list", f"{thread_id}_{i}", max_len=1000)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=pusher, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        items = cache.list_range("ts_list", 0, -1)
        assert len(items) == 500  # 10 threads × 50 pushes

    def test_list_range_empty_key_returns_empty(self, cache: InMemoryCacheAdapter):
        """list_range on non-existent key returns empty list."""
        assert cache.list_range("no_such_key", 0, -1) == []

    def test_list_range_subset(self, cache: InMemoryCacheAdapter):
        """list_range with start/end returns correct subset."""
        for i in range(5):
            cache.push_limit("sub", i, max_len=10)
        assert cache.list_range("sub", 1, 3) == [1, 2, 3]

    def test_list_range_end_minus_one_returns_all_from_start(
        self, cache: InMemoryCacheAdapter
    ):
        """list_range with end=-1 returns all elements from start."""
        for i in range(5):
            cache.push_limit("r", i, max_len=10)
        assert cache.list_range("r", 2, -1) == [2, 3, 4]

    def test_list_range_on_non_list_returns_empty(self, cache: InMemoryCacheAdapter):
        """list_range on a key holding non-list value returns empty list."""
        cache.set("str_key", "not_a_list")
        assert cache.list_range("str_key", 0, -1) == []

    def test_list_range_on_expired_key_returns_empty(self, cache: InMemoryCacheAdapter):
        """list_range on expired key returns empty list."""
        cache.push_limit("exp_r", "v", max_len=10, ttl=timedelta(seconds=0.05))
        time.sleep(0.1)
        assert cache.list_range("exp_r", 0, -1) == []

    # =========================================================================
    # #415 — Adapter-level visibility for swallowed errors
    # =========================================================================

    def test_push_limit_records_operation_error_on_swallow(
        self, cache: InMemoryCacheAdapter
    ):
        """push_limit increments cache_operation_errors_total{backend=memory,operation=push_limit}
        in its swallow branch (#415 — adapter-level visibility for swallowed errors).
        """

        # Force an internal failure: make _make_key raise (called inside the try block)
        with patch.object(
            cache, "_make_key", side_effect=RuntimeError("key build failed")
        ):
            with patch(
                "baldur.metrics.drift_metrics.record_cache_operation_error"
            ) as mock_record:
                result = cache.push_limit("k", "v", max_len=10)

        assert result == 0  # safe default returned
        mock_record.assert_called_once_with(backend="memory", operation="push_limit")

    def test_list_range_records_operation_error_on_swallow(
        self, cache: InMemoryCacheAdapter
    ):
        """list_range increments cache_operation_errors_total{backend=memory,operation=list_range}
        in its swallow branch (#415 — adapter-level visibility for swallowed errors).
        """

        with patch.object(
            cache, "_make_key", side_effect=RuntimeError("key build failed")
        ):
            with patch(
                "baldur.metrics.drift_metrics.record_cache_operation_error"
            ) as mock_record:
                result = cache.list_range("k", 0, -1)

        assert result == []  # safe default returned
        mock_record.assert_called_once_with(backend="memory", operation="list_range")


class TestInMemoryLock:
    """
    InMemoryLock tests.

    Note: InMemoryLock only works within a single process.
    Use the Redis-based distributed lock for multi-process environments.
    """

    def test_lock_repr(self):
        """Lock string representation works without errors."""
        from datetime import timedelta

        dist_lock = InMemoryLock(full_key="test_lock", timeout=timedelta(seconds=10))
        # repr must not raise
        repr(dist_lock)

    def test_double_release(self):
        """
        Double release does not raise.

        Releasing a lock twice must be safe (idempotent).
        """
        from datetime import timedelta

        dist_lock = InMemoryLock(full_key="test_lock", timeout=timedelta(seconds=10))
        dist_lock.acquire()
        dist_lock.release()
        # The second release must also complete without raising
        dist_lock.release()


class TestCacheProviderInterfaceContract:
    """Interface contract compliance tests."""

    def test_abstract_methods_required(self):
        """Cannot instantiate without implementing abstract methods."""
        with pytest.raises(TypeError):
            CacheProviderInterface()

    def test_interface_has_required_methods(self):
        """Required methods are defined on the interface."""
        required_methods = [
            "provider_name",
            "get",
            "set",
            "delete",
            "exists",
            "incr",
            "decr",
            "expire",
            "ttl",
            "get_lock",
            "mget",
            "mset",
            "health_check",
            "flush_all",
            "push_limit",
            "list_range",
            "cas_dict_field",
        ]
        for method in required_methods:
            assert hasattr(CacheProviderInterface, method)


# =============================================================================
# CacheProviderInterface.cas_dict_field — base default (491 D8 / D9)
# =============================================================================


class _MinimalDictCache(CacheProviderInterface):
    """Minimal CacheProviderInterface stub backed by a plain dict.

    Only implements ``get`` / ``set`` / ``delete`` / ``exists`` so the
    inherited base ``cas_dict_field`` (non-atomic ``get → check → set``)
    can be exercised in isolation. All other abstract methods are stubs.
    """

    def __init__(self) -> None:
        self._store: dict[str, object] = {}
        self.set_calls: list[tuple] = []

    @property
    def provider_name(self) -> str:
        return "minimal"

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, ttl=None):
        self.set_calls.append((key, value, ttl))
        self._store[key] = value
        return True

    def delete(self, key):
        return self._store.pop(key, None) is not None

    def exists(self, key):
        return key in self._store

    def incr(self, key, amount=1):  # pragma: no cover
        return 0

    def decr(self, key, amount=1):  # pragma: no cover
        return 0

    def expire(self, key, ttl):  # pragma: no cover
        return False

    def ttl(self, key):  # pragma: no cover
        return -2

    def get_lock(self, name, timeout=None, blocking_timeout=None):  # pragma: no cover
        raise NotImplementedError

    def mget(self, keys):  # pragma: no cover
        return {}

    def mset(self, mapping, ttl=None):  # pragma: no cover
        return True

    def health_check(self):  # pragma: no cover
        return True

    def flush_all(self):  # pragma: no cover
        return True


class TestCacheProviderBaseCasDictFieldBehavior:
    """Base ``cas_dict_field`` non-atomic default — boundary across status states.

    491 D8 / D9: every status transition the production gate cares about
    (executing → completed / failed / non-existent / non-dict / mismatched
    field) must produce the documented return value when the adapter
    inherits the default. Production adapters (Redis, Memory) override
    with atomic implementations, but the base default is the fall-back
    contract surfaced in the interface docstring.
    """

    @pytest.fixture
    def cache(self) -> _MinimalDictCache:
        return _MinimalDictCache()

    def test_returns_true_and_replaces_when_field_matches(self, cache):
        """field == expected → record replaced, returns True."""
        cache._store["k"] = {"status": "executing", "retry_count": 0}
        new_value = {"status": "completed", "result": {"ok": True}}

        ok = cache.cas_dict_field("k", "status", "executing", new_value)

        assert ok is True
        assert cache._store["k"] == new_value

    def test_returns_false_when_key_missing(self, cache):
        """Missing key -> False, no set call."""
        ok = cache.cas_dict_field(
            "missing", "status", "executing", {"status": "completed"}
        )

        assert ok is False
        assert cache.set_calls == []

    def test_returns_false_when_value_is_not_dict(self, cache):
        """Existing value that is not a dict -> False."""
        cache._store["k"] = "not-a-dict"

        ok = cache.cas_dict_field("k", "status", "executing", {"status": "completed"})

        assert ok is False
        assert cache._store["k"] == "not-a-dict"

    def test_returns_false_when_field_does_not_match_expected(self, cache):
        """Field value mismatch -> False, existing record preserved."""
        cache._store["k"] = {"status": "completed", "retry_count": 0}

        ok = cache.cas_dict_field("k", "status", "executing", {"status": "failed"})

        assert ok is False
        assert cache._store["k"] == {"status": "completed", "retry_count": 0}

    def test_returns_false_when_field_absent_from_record(self, cache):
        """Field absent from the record entirely -> False."""
        cache._store["k"] = {"other": "value"}

        ok = cache.cas_dict_field("k", "status", "executing", {"status": "completed"})

        assert ok is False

    def test_forwards_ttl_to_underlying_set(self, cache):
        """On match, ttl is forwarded to self.set."""
        cache._store["k"] = {"status": "executing"}
        ttl = timedelta(seconds=42)

        ok = cache.cas_dict_field(
            "k", "status", "executing", {"status": "completed"}, ttl
        )

        assert ok is True
        assert cache.set_calls[-1] == ("k", {"status": "completed"}, ttl)


# =============================================================================
# InMemoryCacheAdapter.cas_dict_field — atomic override (491 D2)
# =============================================================================


class TestInMemoryCasDictFieldBehavior:
    """``InMemoryCacheAdapter.cas_dict_field`` lock-wrapped override (491 D2)."""

    @pytest.fixture
    def cache(self) -> InMemoryCacheAdapter:
        return InMemoryCacheAdapter(key_prefix="cas:")

    def test_executing_to_completed_returns_true(self, cache):
        """executing → completed transition succeeds."""
        cache.set("k", {"status": "executing", "started_at": 100.0})
        new_value = {"status": "completed", "result": {"ok": True}}

        ok = cache.cas_dict_field("k", "status", "executing", new_value)

        assert ok is True
        assert cache.get("k") == new_value

    def test_existing_completed_returns_false(self, cache):
        """Record already in completed status rejects the cas -> False."""
        cache.set("k", {"status": "completed", "result": {"prev": True}})

        ok = cache.cas_dict_field("k", "status", "executing", {"status": "failed"})

        assert ok is False
        assert cache.get("k") == {"status": "completed", "result": {"prev": True}}

    def test_existing_failed_returns_false(self, cache):
        """In failed status, a cas expecting executing is rejected -> False."""
        cache.set("k", {"status": "failed", "error": "boom"})

        ok = cache.cas_dict_field("k", "status", "executing", {"status": "completed"})

        assert ok is False

    def test_missing_key_returns_false(self, cache):
        """Missing key -> False (atomic store-if-exists semantics)."""
        ok = cache.cas_dict_field(
            "absent", "status", "executing", {"status": "completed"}
        )

        assert ok is False
        assert cache.get("absent") is None

    def test_non_dict_value_returns_false(self, cache):
        """Existing value that is not a dict -> False."""
        cache.set("k", "scalar-value")

        ok = cache.cas_dict_field("k", "status", "executing", {"status": "completed"})

        assert ok is False
        assert cache.get("k") == "scalar-value"

    def test_expired_key_returns_false(self, cache):
        """TTL-expired key -> False (cleanup_expired-equivalent path)."""
        cache.set(
            "k",
            {"status": "executing"},
            ttl=timedelta(milliseconds=50),
        )
        time.sleep(0.1)

        ok = cache.cas_dict_field("k", "status", "executing", {"status": "completed"})

        assert ok is False

    def test_ttl_is_applied_to_replacement_record(self, cache):
        """The ttl argument is applied to the replacement record."""
        cache.set("k", {"status": "executing"})

        ok = cache.cas_dict_field(
            "k",
            "status",
            "executing",
            {"status": "completed"},
            ttl=timedelta(seconds=5),
        )

        assert ok is True
        remaining = cache.ttl("k")
        assert remaining is not None
        assert 3 <= remaining <= 5

    def test_concurrent_cas_only_one_winner(self, cache):
        """Only one competing thread wins the cas (lock-wrapped atomicity)."""
        cache.set("k", {"status": "executing", "thread": -1})
        winners: list[int] = []

        def attempt(thread_id: int) -> None:
            ok = cache.cas_dict_field(
                "k",
                "status",
                "executing",
                {"status": "completed", "thread": thread_id},
            )
            if ok:
                winners.append(thread_id)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1
        final = cache.get("k")
        assert final["status"] == "completed"
        assert final["thread"] == winners[0]


# =============================================================================
# CacheProviderInterface.get_or_set — stampede-safe singleflight (doc 594 D3)
# =============================================================================


class TestGetOrSetContract:
    """D10 design contract: per-call parameters, no settings surface."""

    def test_signature_defaults_and_keyword_only(self):
        """lock_timeout=10s / wait_timeout=10.0, both keyword-only."""
        sig = inspect.signature(CacheProviderInterface.get_or_set)

        lock_timeout = sig.parameters["lock_timeout"]
        wait_timeout = sig.parameters["wait_timeout"]

        assert lock_timeout.default == timedelta(seconds=10)  # design contract
        assert lock_timeout.kind is inspect.Parameter.KEYWORD_ONLY
        assert wait_timeout.default == 10.0  # design contract
        assert wait_timeout.kind is inspect.Parameter.KEYWORD_ONLY


class TestGetOrSetSingleflightBehavior:
    """Winner/loser/timeout paths of the stampede-safe miss dance."""

    @pytest.fixture
    def cache(self) -> InMemoryCacheAdapter:
        return InMemoryCacheAdapter(key_prefix="sf:")

    def test_hit_returns_cached_without_calling_factory(self, cache):
        """The fast-path hit never enters the miss dance."""
        cache.set("k", "cached")
        factory_calls: list[int] = []

        def factory() -> str:
            factory_calls.append(1)
            return "fresh"

        assert cache.get_or_set("k", factory) == "cached"
        assert factory_calls == []

    def test_miss_computes_once_and_stores_with_ttl(self, cache):
        """A lone miss runs the factory once and stores the value with ttl."""
        factory_calls: list[int] = []

        def factory() -> str:
            factory_calls.append(1)
            return "computed"

        result = cache.get_or_set("k", factory, ttl=timedelta(seconds=30))

        assert result == "computed"
        assert factory_calls == [1]
        assert cache.get("k") == "computed"
        remaining = cache.ttl("k")
        assert remaining is not None
        assert 28 <= remaining <= 30

    def test_factory_returning_none_keeps_miss_sentinel_semantics(self, cache):
        """None is stored as before, but reads of it count as misses."""
        calls: list[int] = []

        def none_factory() -> None:
            calls.append(1)
            return None

        first = cache.get_or_set("k", none_factory)
        second = cache.get_or_set("k", none_factory)

        assert first is None
        assert second is None
        assert len(calls) == 2  # a stored None reads as a miss -> recomputed

    def test_concurrent_callers_run_factory_exactly_once(self, cache):
        """N threads missing one key -> 1 factory run, all share the value.

        Deterministic: overlapping threads dedup through the in-process
        funnel, and a late arrival is caught by either the fast-path get
        or the winner's double-check under the distributed lock.
        """
        # Given
        n_threads = 8
        factory_calls: list[int] = []
        results: list[str] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(n_threads + 1)  # +1 for the main thread
        release = threading.Event()

        def gated_factory() -> str:
            factory_calls.append(1)  # winner-only
            release.wait(timeout=5.0)
            return "computed"

        def worker() -> None:
            try:
                barrier.wait(timeout=5.0)
                value = cache.get_or_set("hot-key", gated_factory)
                with results_lock:
                    results.append(value)
            except Exception as e:  # pragma: no cover - failure diagnostics
                with results_lock:
                    errors.append(e)

        # When
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        barrier.wait(timeout=5.0)
        release.set()
        for t in threads:
            t.join(timeout=10.0)

        # Then
        assert errors == []
        assert len(factory_calls) == 1
        assert results == ["computed"] * n_threads
        assert cache.get("hot-key") == "computed"

    def test_loser_polling_returns_winner_value_without_computing(self, cache):
        """A lock-blocked loser returns the winner's value via the value poll.

        The "winner" is simulated: the singleflight lock is held externally
        and the value is published only after the loser has observed its
        fast-path miss - forcing the polling branch deterministically.
        """
        # Given - the per-key singleflight lock is already held
        blocker = cache.get_lock("singleflight:lock:k")
        assert blocker.acquire(blocking=False) is True

        fast_path_checked = threading.Event()
        real_get = cache.get

        def tracked_get(key: str):
            value = real_get(key)
            fast_path_checked.set()
            return value

        factory_calls: list[int] = []

        def factory() -> str:
            factory_calls.append(1)
            return "from-loser"

        results: list[str] = []

        def caller() -> None:
            results.append(cache.get_or_set("k", factory, wait_timeout=5.0))

        # When - the loser starts polling, then the winner's value lands
        try:
            with patch.object(cache, "get", side_effect=tracked_get):
                t = threading.Thread(target=caller)
                t.start()
                assert fast_path_checked.wait(timeout=5.0)  # miss observed
                cache.set("k", "from-winner")  # winner publishes
                t.join(timeout=10.0)
        finally:
            blocker.release()

        # Then - the loser returned the polled value, factory never ran
        assert results == ["from-winner"]
        assert factory_calls == []

    def test_wait_timeout_expiry_falls_open_to_compute(self, cache):
        """A loser that never sees a value computes anyway after wait_timeout.

        R1 bounded duplication: fail-open beats a blocked caller. The
        WARNING event makes the residual duplication visible.
        """
        # Given - a "winner" that holds the lock but never publishes
        blocker = cache.get_lock("singleflight:lock:k")
        assert blocker.acquire(blocking=False) is True

        factory_calls: list[int] = []

        def factory() -> str:
            factory_calls.append(1)
            return "computed-anyway"

        # When
        try:
            with capture_logs() as cap:
                result = cache.get_or_set("k", factory, wait_timeout=0.4)
        finally:
            blocker.release()

        # Then - bounded duplicate compute + stored + WARNING emitted
        assert result == "computed-anyway"
        assert factory_calls == [1]
        assert cache.get("k") == "computed-anyway"
        timeout_events = [
            e for e in cap if e["event"] == SINGLEFLIGHT_WAIT_TIMEOUT_EVENT
        ]
        assert len(timeout_events) == 1
        assert timeout_events[0]["key"] == "k"

    def test_winner_crash_takeover_via_lock_ttl_expiry(self, cache):
        """A crashed winner (lock held, no value, never released) is
        replaced by a polling loser once the lock TTL self-expires."""
        # Given - a short-TTL lock simulating a winner that died mid-compute
        crashed = cache.get_lock(
            "singleflight:lock:k", timeout=timedelta(milliseconds=300)
        )
        assert crashed.acquire(blocking=False) is True

        factory_calls: list[int] = []

        def factory() -> str:
            factory_calls.append(1)
            return "recovered"

        # When - the loser polls past the lock TTL and takes over
        result = cache.get_or_set("k", factory, wait_timeout=5.0)

        # Then
        assert result == "recovered"
        assert factory_calls == [1]
        assert cache.get("k") == "recovered"


class TestGetOrSetFailOpenBehavior:
    """Backend exception at ANY phase -> fail-open (doc 594 D3 phase matrix)."""

    @pytest.fixture
    def cache(self) -> InMemoryCacheAdapter:
        return InMemoryCacheAdapter(key_prefix="failopen:")

    @staticmethod
    def _backend_failed_phases(cap) -> list[str]:
        return [
            e["phase"] for e in cap if e["event"] == SINGLEFLIGHT_BACKEND_FAILED_EVENT
        ]

    def test_lock_acquire_failure_computes_immediately(self, cache):
        """A down lock backend skips the wait entirely and computes."""
        with patch.object(
            cache, "get_lock", side_effect=ConnectionError("lock backend down")
        ):
            with capture_logs() as cap:
                result = cache.get_or_set("k", lambda: "fail-open-value")

        assert result == "fail-open-value"
        assert cache.get("k") == "fail-open-value"  # best-effort store landed
        assert self._backend_failed_phases(cap) == ["lock_acquire"]

    def test_value_poll_failure_computes_immediately(self, cache):
        """A loser whose value poll raises falls open without further waiting."""
        # Given - loser path forced (lock held), second get() raises
        blocker = cache.get_lock("singleflight:lock:k")
        assert blocker.acquire(blocking=False) is True

        real_get = cache.get
        state = {"calls": 0}

        def flaky_get(key: str):
            state["calls"] += 1
            if state["calls"] == 1:
                return real_get(key)  # fast-path miss
            raise ConnectionError("value backend down")

        # When
        try:
            with patch.object(cache, "get", side_effect=flaky_get):
                with capture_logs() as cap:
                    result = cache.get_or_set(
                        "k", lambda: "fail-open-value", wait_timeout=5.0
                    )
        finally:
            blocker.release()

        # Then
        assert result == "fail-open-value"
        assert self._backend_failed_phases(cap) == ["value_poll"]

    def test_store_failure_returns_computed_value_anyway(self, cache):
        """Cache fill is best-effort: a set() failure never discards the value."""
        with patch.object(
            cache, "set", side_effect=ConnectionError("store backend down")
        ):
            with capture_logs() as cap:
                result = cache.get_or_set("k", lambda: "computed")

        assert result == "computed"
        assert cache.get("k") is None  # store failed, value still returned
        assert self._backend_failed_phases(cap) == ["store"]

    def test_lock_release_failure_swallowed_value_returned(self, cache):
        """A release() exception in finally must not replace the winner's
        successful return (the lock self-expires via its TTL)."""
        mock_lock = MagicMock(spec=DistributedLock)
        mock_lock.acquire.return_value = True
        mock_lock.release.side_effect = ConnectionError("connection dropped")

        with patch.object(cache, "get_lock", return_value=mock_lock):
            with capture_logs() as cap:
                result = cache.get_or_set("k", lambda: "computed")

        assert result == "computed"
        assert cache.get("k") == "computed"
        assert self._backend_failed_phases(cap) == ["lock_release"]
        mock_lock.release.assert_called_once()

    def test_factory_exception_propagates_to_caller(self, cache):
        """Fail-open covers backend failures - factory bugs still raise."""

        def failing_factory() -> str:
            raise ValueError("factory bug")

        with pytest.raises(ValueError, match="factory bug"):
            cache.get_or_set("k", failing_factory)

        assert cache.get("k") is None

    def test_lock_backend_down_funnel_still_dedups_in_process(self, cache):
        """Lock backend down + concurrent threads -> the in-process funnel
        still dedups to ONE compute (doc 594 SC2)."""
        # Given - an observable funnel pre-attached at the lazy-attach point
        n_threads = 8
        funnel, all_entered = make_observable_singleflight(n_threads)
        cache._get_or_set_funnel = funnel

        factory_calls: list[int] = []
        results: list[str] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()

        def gated_factory() -> str:
            factory_calls.append(1)  # funnel-winner-only
            all_entered.wait(timeout=5.0)
            return "shared"

        def worker() -> None:
            try:
                value = cache.get_or_set("hot-key", gated_factory)
                with results_lock:
                    results.append(value)
            except Exception as e:  # pragma: no cover - failure diagnostics
                with results_lock:
                    errors.append(e)

        # When - every caller funnels while the lock backend is down
        with patch.object(
            cache, "get_lock", side_effect=ConnectionError("lock backend down")
        ):
            threads = [threading.Thread(target=worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

        # Then - one compute per PROCESS despite the dead lock backend
        assert errors == []
        assert len(factory_calls) == 1
        assert results == ["shared"] * n_threads
