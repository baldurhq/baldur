"""
Redis Cache Adapter Integration Tests

Verifies RedisCacheAdapter operations against a real Redis instance.

Test Categories:
    A. Basic CRUD Operations:
        - set/get/delete/exists lifecycle
        - TTL management (set with TTL, expire, check TTL)
    B. Atomic Operations:
        - incr/decr counters
        - setnx (set if not exists)
    C. Bulk Operations:
        - mget/mset/mdelete with multiple keys
    D. Hash Operations:
        - hset/hget/hgetall
    E. Distributed Locking:
        - Lock acquire/release lifecycle
        - Lock mutual exclusion
        - Lock TTL expiration
        - Lock extend
    F. Health & Maintenance:
        - health_check, reconnect
        - flush_all prefix isolation
        - keys/scan pattern matching
    G. Serialization Roundtrip:
        - Complex Python objects survive serialize/deserialize

Note: All tests require a running Redis instance.
      Marked with @pytest.mark.requires_redis for auto-skip.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter
from baldur.core.test_mode_context import TestModeContext

pytestmark = pytest.mark.requires_redis


@pytest.fixture
def cache(redis_url) -> RedisCacheAdapter:
    """RedisCacheAdapter connected to test Redis."""
    return RedisCacheAdapter(
        url=redis_url,
        key_prefix="test:cache:",
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
    )


# =============================================================================
# A. Basic CRUD Operations
# =============================================================================


class TestBasicCrudOperations:
    """Validates get/set/delete/exists with real Redis."""

    def test_set_and_get_roundtrip(self, cache):
        """
        Purpose:
            Verify basic set/get cycle stores and retrieves data.
        Expected:
            - set returns True
            - get returns the exact stored value
        """
        assert cache.set("key1", {"name": "test", "value": 42}) is True
        result = cache.get("key1")
        assert result == {"name": "test", "value": 42}

    def test_get_nonexistent_key_returns_none(self, cache):
        """
        Purpose:
            Verify get on missing key returns None.
        Expected:
            - None returned, no exception
        """
        assert cache.get("nonexistent") is None

    def test_delete_existing_key(self, cache):
        """
        Purpose:
            Verify delete removes the key and returns True.
        Expected:
            - delete returns True for existing key
            - get returns None after delete
        """
        cache.set("to_delete", "value")
        assert cache.delete("to_delete") is True
        assert cache.get("to_delete") is None

    def test_delete_nonexistent_key_returns_false(self, cache):
        """
        Purpose:
            Verify delete of missing key returns False.
        Expected:
            - delete returns False
        """
        assert cache.delete("never_existed") is False

    def test_exists_for_set_and_missing_keys(self, cache):
        """
        Purpose:
            Verify exists returns correct boolean.
        Expected:
            - True for existing key
            - False for missing key
        """
        cache.set("existing", "value")
        assert cache.exists("existing") is True
        assert cache.exists("missing") is False

    def test_set_with_ttl_expires(self, cache):
        """
        Purpose:
            Verify set with TTL causes key to expire.
        Expected:
            - Key is accessible immediately
            - Key is gone after TTL
        """
        cache.set("ttl_key", "expires_soon", ttl=timedelta(seconds=1))
        assert cache.get("ttl_key") == "expires_soon"
        time.sleep(1.5)
        assert cache.get("ttl_key") is None

    def test_expire_sets_ttl_on_existing_key(self, cache):
        """
        Purpose:
            Verify expire() sets TTL on existing key.
        Expected:
            - expire returns True
            - ttl returns remaining seconds
        """
        cache.set("expire_target", "value")
        assert cache.expire("expire_target", timedelta(seconds=60)) is True
        remaining = cache.ttl("expire_target")
        assert remaining is not None
        assert 50 <= remaining <= 60

    def test_ttl_returns_none_for_no_expiration(self, cache):
        """
        Purpose:
            Verify ttl returns None for key without expiration.
        Expected:
            - ttl returns None
        """
        cache.set("no_ttl", "persistent")
        assert cache.ttl("no_ttl") is None


# =============================================================================
# B. Atomic Operations
# =============================================================================


class TestAtomicOperations:
    """Validates atomic incr/decr/setnx with real Redis."""

    def test_incr_creates_and_increments(self, cache):
        """
        Purpose:
            Verify incr atomically creates and increments counter.
        Expected:
            - First incr returns 1 (auto-create)
            - Second incr returns 2
            - incr with amount adds correctly
        """
        assert cache.incr("counter") == 1
        assert cache.incr("counter") == 2
        assert cache.incr("counter", amount=5) == 7

    def test_decr_decrements(self, cache):
        """
        Purpose:
            Verify decr atomically decrements counter.
        Expected:
            - Counter decrements correctly
        """
        cache.incr("dcounter", amount=10)
        assert cache.decr("dcounter") == 9
        assert cache.decr("dcounter", amount=5) == 4

    def test_setnx_only_sets_if_missing(self, cache):
        """
        Purpose:
            Verify setnx only writes when key doesn't exist.
        Expected:
            - First setnx returns True
            - Second setnx returns False, original value preserved
        """
        assert cache.setnx("nx_key", "first") is True
        assert cache.setnx("nx_key", "second") is False
        assert cache.get("nx_key") == "first"

    def test_setnx_with_ttl(self, cache):
        """
        Purpose:
            Verify setnx with TTL sets expiration.
        Expected:
            - Key exists initially
            - Key expires after TTL
        """
        assert cache.setnx("nx_ttl", "value", ttl=timedelta(seconds=1)) is True
        assert cache.get("nx_ttl") == "value"
        time.sleep(1.5)
        assert cache.get("nx_ttl") is None


# =============================================================================
# C. Bulk Operations
# =============================================================================


class TestBulkOperations:
    """Validates mget/mset/mdelete with real Redis."""

    def test_mset_and_mget(self, cache):
        """
        Purpose:
            Verify mset stores and mget retrieves multiple keys.
        Expected:
            - All keys stored by mset are retrievable via mget
        """
        data = {"bulk1": "val1", "bulk2": "val2", "bulk3": "val3"}
        assert cache.mset(data) is True

        result = cache.mget(["bulk1", "bulk2", "bulk3"])
        assert result == data

    def test_mget_skips_missing_keys(self, cache):
        """
        Purpose:
            Verify mget only returns existing keys.
        Expected:
            - Only existing keys appear in result dict
        """
        cache.set("exists1", "val1")
        result = cache.mget(["exists1", "missing1"])
        assert result == {"exists1": "val1"}

    def test_mset_with_ttl(self, cache):
        """
        Purpose:
            Verify mset with TTL uses pipeline for per-key expiration.
        Expected:
            - Keys expire after TTL
        """
        cache.mset({"ttl1": "a", "ttl2": "b"}, ttl=timedelta(seconds=1))
        assert cache.mget(["ttl1", "ttl2"]) == {"ttl1": "a", "ttl2": "b"}
        time.sleep(1.5)
        assert cache.mget(["ttl1", "ttl2"]) == {}

    def test_mdelete_removes_multiple_keys(self, cache):
        """
        Purpose:
            Verify mdelete removes all specified keys.
        Expected:
            - All keys deleted, returns count
        """
        cache.mset({"del1": "a", "del2": "b", "del3": "c"})
        deleted = cache.mdelete(["del1", "del2", "del3"])
        assert deleted == 3
        assert cache.mget(["del1", "del2", "del3"]) == {}


# =============================================================================
# D. Hash Operations
# =============================================================================


class TestHashOperations:
    """Validates hset/hget/hgetall with real Redis."""

    def test_hset_and_hget(self, cache):
        """
        Purpose:
            Verify hash field set and get operations.
        Expected:
            - hset returns True
            - hget returns the stored value
        """
        assert cache.hset("myhash", "field1", {"nested": "value"}) is True
        assert cache.hget("myhash", "field1") == {"nested": "value"}

    def test_hget_missing_field_returns_none(self, cache):
        """
        Purpose:
            Verify hget on missing field returns None.
        Expected:
            - None returned
        """
        assert cache.hget("empty_hash", "missing_field") is None

    def test_hgetall_returns_all_fields(self, cache):
        """
        Purpose:
            Verify hgetall returns all fields in the hash.
        Expected:
            - All set fields are returned in the dict
        """
        cache.hset("fullhash", "f1", "v1")
        cache.hset("fullhash", "f2", "v2")
        cache.hset("fullhash", "f3", "v3")

        result = cache.hgetall("fullhash")
        assert result == {"f1": "v1", "f2": "v2", "f3": "v3"}


# =============================================================================
# E. Distributed Locking
# =============================================================================


class TestDistributedLocking:
    """Validates RedisDistributedLock with real Redis."""

    def test_lock_acquire_and_release(self, cache):
        """
        Purpose:
            Verify lock can be acquired and released.
        Expected:
            - acquire returns True
            - owned returns True while held
            - After release, locked returns False
        """
        lock = cache.get_lock("test_lock", timeout=timedelta(seconds=5))
        assert lock.acquire(blocking=False) is True
        assert lock.owned() is True
        assert lock.locked() is True

        lock.release()
        assert lock.owned() is False

    def test_lock_mutual_exclusion(self, cache):
        """
        Purpose:
            Verify two lock instances on same name are mutually exclusive.
        Expected:
            - First lock acquires successfully
            - Second lock fails to acquire (non-blocking)
        """
        lock_a = cache.get_lock("exclusive_lock", timeout=timedelta(seconds=5))
        lock_b = cache.get_lock("exclusive_lock", timeout=timedelta(seconds=5))

        assert lock_a.acquire(blocking=False) is True
        assert lock_b.acquire(blocking=False) is False

        lock_a.release()
        assert lock_b.acquire(blocking=False) is True
        lock_b.release()

    def test_lock_auto_expires(self, cache):
        """
        Purpose:
            Verify lock expires after timeout (prevents deadlocks).
        Expected:
            - Lock expires after TTL
            - Another lock can then acquire
        """
        lock_a = cache.get_lock("expire_lock", timeout=timedelta(seconds=1))
        lock_b = cache.get_lock("expire_lock", timeout=timedelta(seconds=5))

        assert lock_a.acquire(blocking=False) is True
        time.sleep(1.5)
        # Lock expired, b can now acquire
        assert lock_b.acquire(blocking=False) is True
        lock_b.release()

    def test_lock_extend_ttl(self, cache):
        """
        Purpose:
            Verify lock TTL can be extended while held.
        Expected:
            - extend returns True
            - Lock remains valid after original TTL
        """
        lock = cache.get_lock("extend_lock", timeout=timedelta(seconds=2))
        assert lock.acquire(blocking=False) is True
        assert lock.extend(timedelta(seconds=10)) is True

        time.sleep(2.5)
        # Lock should still be held after original TTL
        assert lock.owned() is True
        lock.release()

    def test_lock_release_is_atomic(self, cache):
        """
        Purpose:
            Verify release only deletes key if owner matches (Lua atomic).
        Expected:
            - After release, key is removed
            - locked() returns False
        """
        lock = cache.get_lock("atomic_release", timeout=timedelta(seconds=5))
        lock.acquire(blocking=False)
        lock.release()
        assert lock.locked() is False

    def test_test_mode_lock_does_not_collide_with_real_lock(
        self, redis_url, redis_test_client
    ):
        """
        Purpose:
            Verify the #465 X-Test-Mode v1.0 PRO blocker is fixed end-to-end.
            Production-mode and TestModeContext locks at the same name must
            target distinct Redis keys (``baldur:foo`` vs ``xtest:baldur:foo``)
            and acquire independently.
        Expected:
            - Both locks acquire successfully (no mutual exclusion)
            - Two distinct Redis keys exist after acquisition
            - Each key carries its own owner token
        """
        # Dynamic-prefix adapter (key_prefix=None) — honors TestModeContext
        # per #463 D9. Cleanup between tests is handled by the autouse
        # ``_cleanup_between_tests`` fixture (flushdb + factory reset).
        cache = RedisCacheAdapter(url=redis_url, key_prefix=None)

        # Acquire in real mode → expected key: "baldur:xtest_isolation"
        real_lock = cache.get_lock("xtest_isolation", timeout=timedelta(seconds=5))
        assert real_lock.acquire(blocking=False) is True

        # Acquire in synthetic mode → expected key: "xtest:baldur:xtest_isolation"
        with TestModeContext.start(session_id="xtest-465-blocker"):
            synth_lock = cache.get_lock("xtest_isolation", timeout=timedelta(seconds=5))
            # Must succeed — distinct Redis key, no collision with real_lock.
            assert synth_lock.acquire(blocking=False) is True

        # Verify both keys exist with distinct owner tokens.
        real_owner = redis_test_client.get("baldur:xtest_isolation")
        synth_owner = redis_test_client.get("xtest:baldur:xtest_isolation")
        assert real_owner is not None
        assert synth_owner is not None
        assert real_owner != synth_owner

        real_lock.release()
        synth_lock.release()


# =============================================================================
# F. Health & Maintenance
# =============================================================================


class TestHealthAndMaintenance:
    """Validates health_check, flush_all, keys, scan, reconnect."""

    def test_health_check_returns_true(self, cache):
        """
        Purpose:
            Verify health_check pings Redis successfully.
        Expected:
            - Returns True
        """
        assert cache.health_check() is True

    def test_flush_all_only_deletes_prefixed_keys(self, cache, redis_test_client):
        """
        Purpose:
            Verify flush_all only removes keys with the adapter's prefix.
        Expected:
            - Prefixed keys are deleted
            - Non-prefixed keys survive
        """
        # Set keys with adapter prefix
        cache.set("flush_me_1", "val1")
        cache.set("flush_me_2", "val2")
        # Set a key without prefix (directly via raw client)
        redis_test_client.set("unrelated:key", "should_survive")

        assert cache.flush_all() is True
        assert cache.get("flush_me_1") is None
        assert cache.get("flush_me_2") is None
        assert redis_test_client.get("unrelated:key") == "should_survive"

    def test_keys_returns_matching_pattern(self, cache):
        """
        Purpose:
            Verify keys() returns keys matching pattern.
        Expected:
            - Matching keys returned without prefix
        """
        cache.set("search:a", "1")
        cache.set("search:b", "2")
        cache.set("other:c", "3")

        matching = cache.keys("search:*")
        assert set(matching) == {"search:a", "search:b"}

    def test_scan_returns_keys(self, cache):
        """
        Purpose:
            Verify scan() returns keys in the DB.
        Expected:
            - Returns cursor and key list
        """
        for i in range(5):
            cache.set(f"scankey:{i}", f"val{i}")

        cursor, keys = cache.scan("scankey:*", count=100)
        assert len(keys) == 5

    def test_reconnect_succeeds(self, cache):
        """
        Purpose:
            Verify reconnect() re-establishes connection pool.
        Expected:
            - reconnect returns True
            - Operations work after reconnect
        """
        assert cache.reconnect() is True
        cache.set("after_reconnect", "works")
        assert cache.get("after_reconnect") == "works"


# =============================================================================
# G. Serialization Roundtrip
# =============================================================================


class TestSerializationRoundtrip:
    """Validates complex data types survive serialize/deserialize."""

    def test_nested_dict_roundtrip(self, cache):
        """
        Purpose:
            Verify nested dict preserves structure through Redis.
        Expected:
            - Nested structure is identical after roundtrip
        """
        data = {
            "user": {"name": "Alice", "roles": ["admin", "user"]},
            "config": {"max_retries": 3, "timeout": 5.5},
        }
        cache.set("nested", data)
        assert cache.get("nested") == data

    def test_list_roundtrip(self, cache):
        """
        Purpose:
            Verify list data preserves order and values.
        Expected:
            - List is identical after roundtrip
        """
        data = [1, "two", 3.0, None, True]
        cache.set("list_data", data)
        assert cache.get("list_data") == data

    def test_numeric_types_roundtrip(self, cache):
        """
        Purpose:
            Verify int, float, bool values roundtrip correctly.
        Expected:
            - Types and values preserved
        """
        cache.set("int_val", 42)
        cache.set("float_val", 3.14)
        cache.set("bool_val", True)

        assert cache.get("int_val") == 42
        assert cache.get("float_val") == 3.14
        assert cache.get("bool_val") is True

    def test_string_roundtrip(self, cache):
        """
        Purpose:
            Verify plain string values roundtrip correctly.
        Expected:
            - String is identical after roundtrip
        """
        cache.set("str_val", "hello world")
        assert cache.get("str_val") == "hello world"
