"""
In-Memory Cache Adapter for Baldur System

Thread-safe in-memory implementation of CacheProviderInterface.
Designed for testing and development - NOT for production use.

Features:
    - Thread-safe operations with locks
    - TTL support with automatic expiration
    - Distributed lock simulation (single-process only)
    - Full interface compliance

Warning:
    This adapter is for TESTING ONLY. It does not persist data
    and locks are only effective within a single process.

Version: 6.5.0 - Drift Detection metrics moved to MetricsAwareCacheAdapter
"""

from __future__ import annotations

import fnmatch
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar  # noqa: F401

import structlog

from baldur.interfaces.cache_provider import (
    CacheProviderInterface,
    DistributedLock,
    generate_lock_owner_id,
)

logger = structlog.get_logger()


def _record_operation_error(operation: str) -> None:
    """Record a swallowed memory cache operation error (graceful if metrics unavailable)."""
    try:
        from baldur.metrics.drift_metrics import record_cache_operation_error

        record_cache_operation_error(backend="memory", operation=operation)
    except Exception:
        pass


@dataclass
class CacheEntry:
    """Internal cache entry with value and expiration."""

    value: Any
    expires_at: float | None = None  # Unix timestamp

    def is_expired(self) -> bool:
        """Check if entry has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


class InMemoryLock(DistributedLock):
    """
    In-memory distributed lock simulation.

    WARNING: Only works within a single process.
    For multi-process scenarios, use RedisDistributedLock.
    """

    # Class-level lock registry
    _locks: dict[str, InMemoryLock] = {}
    _registry_lock = threading.Lock()

    def __init__(
        self,
        full_key: str,
        timeout: timedelta = timedelta(seconds=10),
        blocking_timeout: float | None = None,
    ) -> None:
        """
        Initialize in-memory lock.

        Args:
            full_key: Verbatim registry key (caller-resolved, used as-is
                for the class-level ``_locks`` dict).
                ``InMemoryCacheAdapter.get_lock()`` resolves
                ``self._make_key(name)`` once and passes the result here.
                Two adapter instances with different ``key_prefix`` end
                up with distinct registry keys for the same user-facing
                lock name, eliminating cross-instance collisions.
            timeout: Lock auto-release timeout
            blocking_timeout: Max time to wait when acquiring
        """
        self._name = full_key
        self._timeout = timeout
        self._blocking_timeout = blocking_timeout
        self._owner_id = generate_lock_owner_id()
        self._lock = threading.Lock()
        self._acquired = False
        self._expires_at: float | None = None

    def acquire(
        self,
        blocking: bool = True,
        timeout: float | None = None,
    ) -> bool:
        """
        Acquire the lock.

        Args:
            blocking: If True, retry until acquired or timeout
            timeout: Override blocking_timeout

        Returns:
            True if lock was acquired
        """
        blocking_timeout = timeout if timeout is not None else self._blocking_timeout
        stop_time = None

        if blocking and blocking_timeout is not None:
            stop_time = time.time() + blocking_timeout

        while True:
            with InMemoryLock._registry_lock:
                # Check for existing lock
                existing = InMemoryLock._locks.get(self._name)

                if existing is None or existing._is_expired():
                    # Clean up expired lock
                    if existing is not None:
                        del InMemoryLock._locks[self._name]

                    # Acquire lock
                    self._expires_at = time.time() + self._timeout.total_seconds()
                    self._acquired = True
                    InMemoryLock._locks[self._name] = self
                    logger.debug(
                        "in_memory_lock.acquired_lock",
                        name=self._name,
                    )
                    return True

            if not blocking:
                return False

            if stop_time is not None and time.time() >= stop_time:
                logger.debug(
                    "in_memory_lock.timeout_acquiring_lock",
                    name=self._name,
                )
                return False

            time.sleep(0.01)  # Short sleep between retries

    def _is_expired(self) -> bool:
        """Check if lock has expired."""
        if self._expires_at is None:
            return True
        return time.time() > self._expires_at

    def release(self) -> None:
        """Release the lock."""
        with InMemoryLock._registry_lock:
            if self._name in InMemoryLock._locks:
                current = InMemoryLock._locks[self._name]
                if current._owner_id == self._owner_id:
                    del InMemoryLock._locks[self._name]
                    self._acquired = False
                    logger.debug(
                        "in_memory_lock.released_lock",
                        name=self._name,
                    )
                else:
                    logger.warning(
                        "in_memory_lock.lock_owned",
                        name=self._name,
                    )
            else:
                self._acquired = False

    def locked(self) -> bool:
        """Check if lock is currently held by anyone."""
        with InMemoryLock._registry_lock:
            existing = InMemoryLock._locks.get(self._name)
            if existing is None:
                return False
            if existing._is_expired():
                del InMemoryLock._locks[self._name]
                return False
            return True

    def owned(self) -> bool:
        """Check if lock is held by this instance."""
        with InMemoryLock._registry_lock:
            existing = InMemoryLock._locks.get(self._name)
            if existing is None:
                return False
            if existing._is_expired():
                del InMemoryLock._locks[self._name]
                return False
            return existing._owner_id == self._owner_id

    def extend(self, additional_time: timedelta) -> bool:
        """Extend the lock's TTL (owner-fenced).

        The ownership/expiry check is inlined inside the held registry
        lock: delegating to ``owned()`` would re-enter the non-reentrant
        ``_registry_lock`` and hang the caller. Returns ``False`` when the
        registry entry is missing, expired, or owned by another instance.
        """
        with InMemoryLock._registry_lock:
            existing = InMemoryLock._locks.get(self._name)
            if existing is None or existing._is_expired():
                return False
            if existing._owner_id != self._owner_id:
                return False
            self._expires_at = time.time() + additional_time.total_seconds()
            return True

    @classmethod
    def clear_all_locks(cls) -> None:
        """Clear all locks (for testing cleanup)."""
        with cls._registry_lock:
            cls._locks.clear()


class InMemoryCacheAdapter(CacheProviderInterface):
    """
    Thread-safe in-memory cache implementation.

    This adapter provides a complete cache implementation using
    Python dictionaries with thread-safe operations.

    Features:
        - Full CacheProviderInterface compliance
        - Thread-safe operations
        - TTL support with lazy expiration
        - Mock distributed locks (single-process only)

    Example:
        >>> cache = InMemoryCacheAdapter()
        >>> cache.set("key", {"data": "value"}, ttl=timedelta(minutes=5))
        >>> data = cache.get("key")
        >>> with cache.get_lock("my_lock") as lock:
        ...     # Critical section (only within single process!)
        ...     pass

    Warning:
        This is for TESTING ONLY. Data is not persisted and
        locks only work within a single process.
    """

    # Class-level weak registry of all live adapter instances. Populated in
    # __init__; entries auto-disappear when the instance is GC'd. Used by
    # CleanupService.cleanup_memory_cache_expired() to drive periodic
    # expiration sweeps across every live instance — covers ad-hoc fallbacks
    # in decorators/idempotent.py and services/security/* that aren't
    # registered with ProviderRegistry.cache.
    _instances: ClassVar[weakref.WeakSet[InMemoryCacheAdapter]] = weakref.WeakSet()

    def __init__(self, key_prefix: str = "test:", cache_name: str = "memory") -> None:
        """
        Initialize in-memory cache.

        Args:
            key_prefix: Prefix for all cache keys
            cache_name: Name for metrics identification
        """
        self._key_prefix = key_prefix
        self._cache_name = cache_name
        self._store: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._healthy = True
        InMemoryCacheAdapter._instances.add(self)

    def _make_key(self, key: str) -> str:
        """Add prefix to key."""
        return f"{self._key_prefix}{key}"

    def _cleanup_expired(self) -> int:
        """Remove expired entries (assumes ``self._lock`` already held).

        Returns the number of entries removed. Used by ``keys()`` and
        ``get_store_size()`` which both already hold ``self._lock``;
        ``self._lock`` is non-reentrant ``threading.Lock``, so external
        callers must use the public ``cleanup_expired()`` instead.
        """
        current_time = time.time()
        expired_keys = [
            k
            for k, v in self._store.items()
            if v.expires_at is not None and v.expires_at < current_time
        ]
        for key in expired_keys:
            del self._store[key]
        return len(expired_keys)

    def cleanup_expired(self) -> int:
        """Lock-acquiring entry point for periodic expiration sweeps.

        Wraps the unlocked ``_cleanup_expired()`` with ``self._lock`` so
        external callers (CleanupService) can safely invoke it. ``self._lock``
        is a non-reentrant ``threading.Lock``; reusing the private method from
        a non-locked context would deadlock.

        Returns:
            Number of expired entries removed.
        """
        with self._lock:
            return self._cleanup_expired()

    @classmethod
    def clear_all_instances(cls) -> None:
        """Clear the class-level weak registry (for test fixtures)."""
        cls._instances.clear()

    @property
    def provider_name(self) -> str:
        """Return 'memory' as the provider identifier."""
        return "memory"

    # =========================================================================
    # Basic Operations
    # =========================================================================

    def get(self, key: str) -> Any | None:
        """Get value by key."""
        with self._lock:
            full_key = self._make_key(key)
            entry = self._store.get(full_key)

            if entry is None:
                return None

            if entry.is_expired():
                del self._store[full_key]
                return None

            return entry.value

    def set(
        self,
        key: str,
        value: Any,
        ttl: timedelta | None = None,
    ) -> bool:
        """Set value with optional TTL."""
        with self._lock:
            full_key = self._make_key(key)
            expires_at = None
            if ttl is not None:
                expires_at = time.time() + ttl.total_seconds()

            self._store[full_key] = CacheEntry(value=value, expires_at=expires_at)
            return True

    def delete(self, key: str) -> bool:
        """Delete key from cache."""
        with self._lock:
            full_key = self._make_key(key)
            if full_key in self._store:
                del self._store[full_key]
                return True
            return False

    def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        with self._lock:
            full_key = self._make_key(key)
            entry = self._store.get(full_key)

            if entry is None:
                return False

            if entry.is_expired():
                del self._store[full_key]
                return False

            return True

    # =========================================================================
    # Atomic Operations
    # =========================================================================

    def incr(self, key: str, amount: int = 1) -> int:
        """Atomically increment a counter."""
        with self._lock:
            full_key = self._make_key(key)
            entry = self._store.get(full_key)

            if entry is None or entry.is_expired():
                # Create new counter
                self._store[full_key] = CacheEntry(value=amount)
                return amount

            # Increment existing
            new_value = int(entry.value) + amount
            entry.value = new_value
            return new_value

    def decr(self, key: str, amount: int = 1) -> int:
        """Atomically decrement a counter."""
        return self.incr(key, -amount)

    def expire(self, key: str, ttl: timedelta) -> bool:
        """Set expiration on existing key."""
        with self._lock:
            full_key = self._make_key(key)
            entry = self._store.get(full_key)

            if entry is None or entry.is_expired():
                return False

            entry.expires_at = time.time() + ttl.total_seconds()
            return True

    def ttl(self, key: str) -> int | None:
        """Get remaining TTL in seconds."""
        with self._lock:
            full_key = self._make_key(key)
            entry = self._store.get(full_key)

            if entry is None:
                return -2  # Key doesn't exist

            if entry.is_expired():
                del self._store[full_key]
                return -2

            if entry.expires_at is None:
                return None  # No expiration

            remaining = entry.expires_at - time.time()
            return max(0, int(remaining))

    def setnx(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Set value only if key does not exist."""
        with self._lock:
            full_key = self._make_key(key)
            entry = self._store.get(full_key)

            if entry is not None and not entry.is_expired():
                return False

            expires_at = None
            if ttl is not None:
                expires_at = time.time() + ttl.total_seconds()

            self._store[full_key] = CacheEntry(value=value, expires_at=expires_at)
            return True

    def cas_dict_field(
        self,
        key: str,
        field: str,
        expected: Any,
        new_value: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomic single-field CAS on a dict-valued record (lock-wrapped)."""
        with self._lock:
            full_key = self._make_key(key)
            entry = self._store.get(full_key)

            if entry is None or entry.is_expired():
                return False
            if not isinstance(entry.value, dict):
                return False
            if entry.value.get(field) != expected:
                return False

            expires_at = None
            if ttl is not None:
                expires_at = time.time() + ttl.total_seconds()

            self._store[full_key] = CacheEntry(value=new_value, expires_at=expires_at)
            return True

    def cas_takeover(
        self,
        key: str,
        new_record: dict[str, Any],
        *,
        stale_before: float,
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomic failed / stale-executing takeover (lock-wrapped).

        Replaces the record iff it is a dict whose ``status == "failed"`` OR
        (``status == "executing"`` AND ``started_at < stale_before``). No I/O to
        fail, so — like ``setnx`` / ``cas_dict_field`` on this adapter — it never
        raises ``AdapterConnectionError``.
        """
        with self._lock:
            full_key = self._make_key(key)
            entry = self._store.get(full_key)

            if entry is None or entry.is_expired():
                return False
            if not isinstance(entry.value, dict):
                return False

            status = entry.value.get("status")
            takeable = status == "failed" or (
                status == "executing"
                and entry.value.get("started_at", 0) < stale_before
            )
            if not takeable:
                return False

            expires_at = None
            if ttl is not None:
                expires_at = time.time() + ttl.total_seconds()

            self._store[full_key] = CacheEntry(value=new_record, expires_at=expires_at)
            return True

    # =========================================================================
    # Distributed Locking
    # =========================================================================

    def get_lock(
        self,
        name: str,
        timeout: timedelta = timedelta(seconds=10),
        blocking_timeout: float | None = None,
    ) -> DistributedLock:
        """Get a distributed lock instance.

        Resolves the registry key once via ``self._make_key(name)`` so
        two adapter instances with different ``key_prefix`` map the same
        user-facing name to distinct entries in the class-level
        ``InMemoryLock._locks`` registry.
        """
        # #465 D6: resolve the key once here so prefixed instances do not collide.
        return InMemoryLock(
            full_key=self._make_key(name),
            timeout=timeout,
            blocking_timeout=blocking_timeout,
        )

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    def mget(self, keys: list[str]) -> dict[str, Any]:
        """Get multiple values at once."""
        result = {}
        with self._lock:
            for key in keys:
                full_key = self._make_key(key)
                entry = self._store.get(full_key)
                if entry is not None and not entry.is_expired():
                    result[key] = entry.value
        return result

    def mset(
        self,
        mapping: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """Set multiple values at once."""
        with self._lock:
            expires_at = None
            if ttl is not None:
                expires_at = time.time() + ttl.total_seconds()

            for key, value in mapping.items():
                full_key = self._make_key(key)
                self._store[full_key] = CacheEntry(value=value, expires_at=expires_at)
        return True

    def mdelete(self, keys: list[str]) -> int:
        """Delete multiple keys at once."""
        deleted = 0
        with self._lock:
            for key in keys:
                full_key = self._make_key(key)
                if full_key in self._store:
                    del self._store[full_key]
                    deleted += 1
        return deleted

    # =========================================================================
    # List Operations
    # =========================================================================

    def push_limit(
        self, key: str, value: Any, max_len: int, ttl: timedelta | None = None
    ) -> int:
        """Append value to a list and trim to max_len under thread lock."""
        try:
            with self._lock:
                full_key = self._make_key(key)
                entry = self._store.get(full_key)

                if (
                    entry is None
                    or entry.is_expired()
                    or not isinstance(entry.value, list)
                ):
                    lst = []
                else:
                    lst = entry.value

                lst.append(value)
                pre_trim_len = len(lst)
                if len(lst) > max_len:
                    lst = lst[-max_len:]

                expires_at = None
                if ttl is not None:
                    expires_at = time.time() + ttl.total_seconds()
                self._store[full_key] = CacheEntry(value=lst, expires_at=expires_at)
                return pre_trim_len
        except Exception as e:
            logger.exception(
                "memory_cache.push_limit_error",
                cache_key=key,
                error=e,
            )
            _record_operation_error("push_limit")
            return 0

    def list_range(self, key: str, start: int, end: int) -> list[Any]:
        """Return elements from start to end (inclusive)."""
        try:
            with self._lock:
                full_key = self._make_key(key)
                entry = self._store.get(full_key)

                if entry is None or entry.is_expired():
                    return []
                if not isinstance(entry.value, list):
                    return []

                if end == -1:
                    return entry.value[start:]
                return entry.value[start : end + 1]
        except Exception as e:
            logger.exception(
                "memory_cache.list_range_error",
                cache_key=key,
                error=e,
            )
            _record_operation_error("list_range")
            return []

    # =========================================================================
    # Health Check
    # =========================================================================

    def health_check(self) -> bool:
        """Check if cache is healthy."""
        return self._healthy

    def set_health_status(self, healthy: bool) -> None:
        """Set health status for testing."""
        self._healthy = healthy

    def flush_all(self) -> bool:
        """Clear all keys."""
        with self._lock:
            # Only clear keys with our prefix
            keys_to_delete = [k for k in self._store if k.startswith(self._key_prefix)]
            for key in keys_to_delete:
                del self._store[key]
            logger.info(
                "in_memory_cache.flushed_keys",
                keys_to_delete_count=len(keys_to_delete),
            )

        # Also clear locks
        InMemoryLock.clear_all_locks()
        return True

    # =========================================================================
    # Key Pattern Operations
    # =========================================================================

    def keys(self, pattern: str = "*") -> list[str]:
        """Find keys matching a pattern."""
        with self._lock:
            self._cleanup_expired()
            full_pattern = self._make_key(pattern)
            prefix_len = len(self._key_prefix)

            matching = []
            for key in self._store:
                if fnmatch.fnmatch(key, full_pattern):
                    matching.append(key[prefix_len:])

            return matching

    def scan(
        self,
        pattern: str = "*",
        count: int = 100,
    ) -> tuple[int, list[str]]:
        """Incrementally iterate keys matching a pattern."""
        # In-memory implementation just returns all matching keys
        return (0, self.keys(pattern)[:count])

    # =========================================================================
    # Testing Utilities
    # =========================================================================

    def get_store_size(self) -> int:
        """Get number of entries in store (for testing)."""
        with self._lock:
            self._cleanup_expired()
            return len(self._store)

    def clear_all(self) -> None:
        """Clear entire store including all prefixes (for testing cleanup)."""
        with self._lock:
            self._store.clear()
        InMemoryLock.clear_all_locks()
