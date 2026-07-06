"""
Cache Provider Interface for Baldur System

Abstract interface for cache and distributed state management.
Supports distributed locking critical for circuit breakers.

Design Principles:
1. Pure Python - no framework dependencies
2. ABC for provider contracts
3. Context manager support for locks
4. Atomic operations for counters
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import structlog

from baldur.core.exceptions import BaldurError
from baldur.core.singleflight import Singleflight
from baldur.utils.jitter import calculate_jitter

logger = structlog.get_logger()

# Loser value-poll cadence in get_or_set's distributed singleflight.
# Matches DistributedLockSettings.retry_interval_seconds default; the
# explicit jitter args below skip the settings load on the poll path.
_SINGLEFLIGHT_POLL_INTERVAL_SECONDS = 0.1

# Guards lazy attachment of the per-adapter-instance miss funnel
# (CacheProviderInterface has no __init__ to extend).
_singleflight_attach_lock = threading.Lock()


def generate_lock_owner_id() -> str:
    """Standard lock owner ID for all DistributedLock implementations."""
    return (
        f"{socket.gethostname()}:{os.getpid()}"
        f":{threading.get_ident()}:{uuid.uuid4().hex[:8]}"
    )


# ============================================================================
# Distributed Lock Interface
# ============================================================================


class DistributedLock(ABC):
    """
    Distributed lock interface for cross-process synchronization.

    Used by CircuitBreaker for state transitions and other
    critical sections that require mutual exclusion across
    multiple processes or servers.

    Supports context manager protocol for safe usage:

        with cache.get_lock("circuit_breaker:payment") as lock:
            # Critical section - only one process can execute
            circuit_breaker.transition_state()

    Implementations:
        - RedisDistributedLock (Redis-based)
        - InMemoryLock (for testing - single process only)

    Storage-key contract:
        Lock implementations MUST treat the constructor's ``full_key``
        argument as the verbatim storage key. The owning adapter
        (``cache.get_lock(name)``) is the single point that resolves the
        user-supplied lock name into a full key by routing it through its
        own ``_make_key()`` (which honors ``key_prefix``,
        ``TestModeContext``, and ``NamespaceSettings`` — Redis
        only). The lock writes ``full_key`` directly to storage.

        **Anti-pattern**: lock implementations MUST NOT hardcode any
        prefix segment (e.g., ``f"lock:{name}"``) inside ``__init__``.
        Earlier ``RedisDistributedLock`` versions did exactly this, producing
        double-prefixed keys (``baldur:lock:idempotency:lock:order:abc``)
        and bypassing the adapter's prefix system entirely. New adapter
        implementations (DynamoDB / etcd / ZooKeeper) must follow the
        contract: full key in, full key written, no in-class
        transformation.

        SCAN-observability convention: callers that want a recognizable
        ``lock:`` segment in storage should embed it in the name passed
        to ``cache.get_lock()`` (e.g., ``cache.get_lock("idempotency:lock:order:abc")``)
        rather than relying on the lock class to add it.

        Lifecycle: lock construction, ``acquire``, and ``release`` should
        occur within a single ``TestModeContext`` scope. The natural
        ``with cache.get_lock(name) as lock:`` pattern enforces this.
        Crossing a context boundary between construction and acquire
        leaves the lock in the construction-time namespace — safe (no
        orphan) but breaks synthetic isolation for that one lock.
    """

    @abstractmethod
    def acquire(
        self,
        blocking: bool = True,
        timeout: float | None = None,
    ) -> bool:
        """
        Acquire the lock.

        Args:
            blocking: If True, block until lock is acquired
            timeout: Max seconds to wait (None = infinite)

        Returns:
            True if lock was acquired, False otherwise

        Note:
            If blocking=False and lock is held, returns False immediately.
            If blocking=True and timeout expires, returns False.
        """
        pass

    @abstractmethod
    def release(self) -> None:
        """
        Release the lock.

        Raises:
            LockNotOwnedError: If lock is not held by current owner
        """
        pass

    @abstractmethod
    def locked(self) -> bool:
        """
        Check if lock is currently held by anyone.

        Returns:
            True if lock is held, False if available
        """
        pass

    @abstractmethod
    def owned(self) -> bool:
        """
        Check if lock is held by current owner.

        Returns:
            True if lock is held by this instance
        """
        pass

    def extend(self, additional_time: timedelta) -> bool:
        """
        Extend the lock's TTL.

        Args:
            additional_time: Time to add to current TTL

        Returns:
            True if extension was successful

        Note:
            Default implementation returns False (not supported).
            Override in implementations that support TTL extension.
        """
        return False

    def __enter__(self) -> DistributedLock:
        """Enter context manager, acquiring the lock."""
        acquired = self.acquire()
        if not acquired:
            raise LockAcquisitionError("Failed to acquire lock")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context manager, releasing the lock."""
        self.release()


class LockAcquisitionError(BaldurError):
    """Raised when lock acquisition fails."""

    pass


class LockNotOwnedError(BaldurError):
    """Raised when trying to release a lock not owned by current instance."""

    pass


# ============================================================================
# Cache Provider Interface
# ============================================================================


class CacheProviderInterface(ABC):
    """
    Abstract interface for cache/state storage.

    This interface abstracts cache operations including basic
    get/set, atomic counters, and distributed locking.

    Implementations:
        - RedisCacheAdapter (current - Redis)
        - InMemoryCacheAdapter (for testing)
        - MemcachedCacheAdapter (planned)
        - DynamoDBCacheAdapter (planned - AWS serverless)

    Example:
        >>> cache = ProviderRegistry.get_cache()
        >>>
        >>> # Basic operations
        >>> cache.set("key", "value", ttl=timedelta(minutes=5))
        >>> value = cache.get("key")
        >>>
        >>> # Atomic counter (for rate limiting)
        >>> count = cache.incr("request_count")
        >>> if count == 1:
        ...     cache.expire("request_count", timedelta(minutes=1))
        >>>
        >>> # Distributed locking
        >>> with cache.get_lock("payment:process") as lock:
        ...     process_payment()
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """
        Return the provider name.

        Returns:
            Provider identifier (e.g., 'redis', 'memcached', 'memory')
        """
        pass

    # =========================================================================
    # Basic Operations
    # =========================================================================

    @abstractmethod
    def get(self, key: str) -> Any | None:
        """
        Get value by key.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        pass

    @abstractmethod
    def set(
        self,
        key: str,
        value: Any,
        ttl: timedelta | None = None,
    ) -> bool:
        """
        Set value with optional TTL.

        Args:
            key: Cache key
            value: Value to cache (must be serializable)
            ttl: Time-to-live (None = no expiration)

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """
        Delete key from cache.

        Args:
            key: Cache key to delete

        Returns:
            True if key existed and was deleted
        """
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        """
        Check if key exists in cache.

        Args:
            key: Cache key to check

        Returns:
            True if key exists and is not expired
        """
        pass

    def get_or_set(
        self,
        key: str,
        default_factory: Callable[[], Any],
        ttl: timedelta | None = None,
        *,
        lock_timeout: timedelta = timedelta(seconds=10),
        wait_timeout: float = 10.0,
    ) -> Any:
        """
        Get value or compute and cache it if missing, stampede-safe.

        On a miss, concurrent callers coordinate so the factory runs
        once instead of once per caller (hot-key expiry no longer fans
        out N concurrent factory executions against the backend):

        - In-process, the entire miss path is funneled through a
          per-adapter Singleflight: one thread per process enters the
          distributed dance; sibling threads share its result or
          exception.
        - Across processes, a non-blocking distributed lock elects one
          winner (factory + set); losers poll the VALUE with a jittered
          ~0.1s cadence, retrying the non-blocking acquire so a
          crashed winner is replaced.
        - Fail-open: a lock/cache backend failure at any phase, or
          ``wait_timeout`` expiry, degrades to computing the value
          directly - bounded duplication, never an error or a blocked
          caller. The factory can therefore run more than once under
          winner crash/stall or backend failure; a factory whose
          side-effects must never execute twice has to guard itself
          (e.g., via IdempotencyGate).

        ``None`` keeps its miss-sentinel semantics: a factory returning
        None is stored as before, but reads of it count as misses.

        Args:
            key: Cache key
            default_factory: Callable to compute value if missing
            ttl: Time-to-live for new value
            lock_timeout: Auto-release TTL of the per-key singleflight
                lock; bounds how long a crashed winner blocks takeover
            wait_timeout: Max seconds a loser waits for the winner's
                value before computing it anyway (match this to the
                factory's runtime budget)

        Returns:
            Cached or newly computed value
        """
        value = self.get(key)
        if value is not None:
            return value

        # In-process funnel: only one thread per process enters the
        # distributed dance below; sibling threads share its outcome.
        # This bounds residual duplication per PROCESS (not per thread)
        # and cuts lock-acquire attempts and value-poll QPS to one per
        # process. The fast-path hit above stays outside the funnel.
        return self._miss_singleflight().run(
            key,
            lambda: self._get_or_set_miss(
                key, default_factory, ttl, lock_timeout, wait_timeout
            ),
        )

    def _miss_singleflight(self) -> Singleflight:
        """Lazily attach the per-instance miss funnel (no __init__ to extend)."""
        funnel = getattr(self, "_get_or_set_funnel", None)
        if funnel is None:
            with _singleflight_attach_lock:
                funnel = getattr(self, "_get_or_set_funnel", None)
                if funnel is None:
                    funnel = Singleflight()
                    self._get_or_set_funnel = funnel
        return funnel

    def _get_or_set_miss(
        self,
        key: str,
        default_factory: Callable[[], Any],
        ttl: timedelta | None,
        lock_timeout: timedelta,
        wait_timeout: float,
    ) -> Any:
        """Cross-process miss dance: non-blocking-lock winner + value-polling losers."""
        # `lock:` segment embedded in the name for SCAN observability
        # (see the DistributedLock storage-key contract).
        try:
            lock = self.get_lock(f"singleflight:lock:{key}", timeout=lock_timeout)
            acquired = lock.acquire(blocking=False)
        except Exception as e:
            # A down lock backend means value polling through the same
            # backend is pointless - fail open immediately, no wait.
            logger.warning(
                "cache_provider.singleflight_backend_failed",
                phase="lock_acquire",
                key=key,
                error=str(e),
            )
            return self._compute_and_store_best_effort(key, default_factory, ttl)

        if acquired:
            return self._compute_as_winner(key, default_factory, ttl, lock)

        # Loser: poll the VALUE, not the lock, so every loser returns as
        # soon as the winner's set lands instead of draining a lock
        # queue serially. Each iteration retries the non-blocking
        # acquire so a crashed/failed winner is replaced by the next
        # poller.
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            # +/-20% jitter around the poll interval so simultaneous
            # losers across workers do not phase-align their polls into
            # backend micro-bursts.
            time.sleep(
                calculate_jitter(
                    _SINGLEFLIGHT_POLL_INTERVAL_SECONDS * 1.2,
                    _SINGLEFLIGHT_POLL_INTERVAL_SECONDS * 0.8,
                )
            )
            try:
                value = self.get(key)
            except Exception as e:
                logger.warning(
                    "cache_provider.singleflight_backend_failed",
                    phase="value_poll",
                    key=key,
                    error=str(e),
                )
                return self._compute_and_store_best_effort(key, default_factory, ttl)
            if value is not None:
                return value
            try:
                if lock.acquire(blocking=False):
                    return self._compute_as_winner(key, default_factory, ttl, lock)
            except Exception as e:
                logger.warning(
                    "cache_provider.singleflight_backend_failed",
                    phase="lock_acquire",
                    key=key,
                    error=str(e),
                )
                return self._compute_and_store_best_effort(key, default_factory, ttl)

        # wait_timeout expired: final re-check, then bounded duplicate
        # compute (fail-open; see the docstring's duplication note).
        try:
            value = self.get(key)
            if value is not None:
                return value
        except Exception as e:
            logger.warning(
                "cache_provider.singleflight_backend_failed",
                phase="value_poll",
                key=key,
                error=str(e),
            )
            return self._compute_and_store_best_effort(key, default_factory, ttl)

        logger.warning(
            "cache_provider.singleflight_wait_timeout",
            key=key,
            wait_timeout=wait_timeout,
        )
        return self._compute_and_store_best_effort(key, default_factory, ttl)

    def _compute_as_winner(
        self,
        key: str,
        default_factory: Callable[[], Any],
        ttl: timedelta | None,
        lock: DistributedLock,
    ) -> Any:
        """Singleflight-lock holder: double-check, compute, store, release."""
        try:
            # Double-check under the lock: another winner may have
            # filled the key between this caller's miss and its acquire.
            try:
                value = self.get(key)
                if value is not None:
                    return value
            except Exception as e:
                # Already the winner - computing IS the fail-open path.
                logger.warning(
                    "cache_provider.singleflight_backend_failed",
                    phase="value_poll",
                    key=key,
                    error=str(e),
                )
            return self._compute_and_store_best_effort(key, default_factory, ttl)
        finally:
            try:
                lock.release()
            except Exception as e:
                # Swallowed: an unguarded finally would replace the
                # winner's successful return with the release error
                # (fail-closed). The lock self-expires via its
                # lock_timeout TTL.
                logger.warning(
                    "cache_provider.singleflight_backend_failed",
                    phase="lock_release",
                    key=key,
                    error=str(e),
                )

    def _compute_and_store_best_effort(
        self,
        key: str,
        default_factory: Callable[[], Any],
        ttl: timedelta | None,
    ) -> Any:
        """Run the factory and best-effort cache the result.

        A store failure after a successful compute returns the value
        anyway - discarding a value the factory already produced would
        fail closed. Factory exceptions propagate to the caller (and,
        through the in-process funnel, to its waiters).
        """
        value = default_factory()
        try:
            self.set(key, value, ttl)
        except Exception as e:
            logger.warning(
                "cache_provider.singleflight_backend_failed",
                phase="store",
                key=key,
                error=str(e),
            )
        return value

    # =========================================================================
    # Atomic Operations (Critical for Circuit Breaker)
    # =========================================================================

    @abstractmethod
    def incr(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment a counter.

        Args:
            key: Counter key
            amount: Increment amount (default 1)

        Returns:
            New counter value after increment

        Note:
            Creates key with value 0 if not exists, then increments.
            This is an atomic operation - safe for concurrent access.
        """
        pass

    @abstractmethod
    def decr(self, key: str, amount: int = 1) -> int:
        """
        Atomically decrement a counter.

        Args:
            key: Counter key
            amount: Decrement amount (default 1)

        Returns:
            New counter value after decrement
        """
        pass

    @abstractmethod
    def expire(self, key: str, ttl: timedelta) -> bool:
        """
        Set expiration on existing key.

        Args:
            key: Cache key
            ttl: Time-to-live duration

        Returns:
            True if key exists and expiration was set
        """
        pass

    @abstractmethod
    def ttl(self, key: str) -> int | None:
        """
        Get remaining TTL in seconds.

        Args:
            key: Cache key

        Returns:
            - Positive int: seconds until expiration
            - None: key has no expiration
            - -2: key does not exist
        """
        pass

    def setnx(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """
        Set value only if key does not exist (SET if Not eXists).

        Args:
            key: Cache key
            value: Value to set
            ttl: Optional time-to-live

        Returns:
            True if key was set (didn't exist), False otherwise
        """
        if not self.exists(key):
            return self.set(key, value, ttl)
        return False

    def cas_dict_field(
        self,
        key: str,
        field: str,
        expected: Any,
        new_value: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """
        Atomic compare-and-set on a single field of a dict-valued record.

        Reads the existing record at ``key``; if it is a dict whose
        ``field`` equals ``expected``, replaces the entire record with
        ``new_value`` (with optional TTL). Otherwise, returns False
        without writing.

        Args:
            key: Cache key holding a dict-valued record
            field: Field name within the dict to check
            expected: Expected current value of ``field``
            new_value: Replacement record (full dict, not a partial update)
            ttl: Optional time-to-live for the replacement record

        Returns:
            True if the record matched and was replaced, False otherwise
            (key missing, value not a dict, field mismatch).

        Note:
            The base implementation is a non-atomic ``get → check → set``
            two-step. Production adapters (Redis, Memory) override with
            atomic implementations. ``IdempotencyGate`` validates that an
            atomic override is in use via
            ``_validate_atomic_cas_dict_field`` to prevent silent
            inheritance of the non-atomic default.
        """
        existing = self.get(key)
        if not isinstance(existing, dict):
            return False
        if existing.get(field) != expected:
            return False
        return self.set(key, new_value, ttl)

    def cas_takeover(
        self,
        key: str,
        new_record: dict[str, Any],
        *,
        stale_before: float,
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomically take over a failed / stale-executing dedup claim.

        Reads the record at ``key`` and replaces the whole record with
        ``new_record`` (optional TTL) **iff** the existing value is a dict whose
        ``status == "failed"`` OR (``status == "executing"`` AND
        ``started_at < stale_before``). Otherwise returns False without writing.

        This is the single-round-trip atomic guard for the idempotency gate's
        retry paths. A plain compare on ``status`` alone would be insufficient:
        once a taker rewrites the record to ``executing`` again, a second taker
        would still match ``status == "executing"`` and overwrite the fresh
        claim. Re-checking ``started_at < stale_before`` inside the same atomic
        op makes the fresh claim (whose ``started_at`` is ``now``, not
        ``< stale_before``) ineligible — single-winner without depending on
        serialized-value byte-equality.

        Args:
            key: Cache key holding a dict-valued dedup record.
            new_record: Replacement record (full dict, not a partial update).
            stale_before: App-computed staleness threshold. An ``executing``
                record is takeable only if its ``started_at`` is strictly
                below this value. Computed by the caller as
                ``now - execution_ttl - clock_skew_tolerance`` so the
                comparison stays app-clock vs app-clock (never the backend
                server clock).
            ttl: Optional time-to-live for the replacement record.

        Returns:
            True if the record was failed / stale-executing and was replaced,
            False otherwise (key missing, value not a dict, fresh executing,
            completed, or unknown status).

        Note:
            The base implementation is a non-atomic ``get → check → set``
            two-step. Production adapters (Redis Lua, Memory lock-wrapped)
            override with atomic implementations. ``IdempotencyGate`` validates
            that an atomic override is in use via
            ``_validate_atomic_cas_takeover`` to prevent silent inheritance of
            the non-atomic default.
        """
        existing = self.get(key)
        if not isinstance(existing, dict):
            return False
        status = existing.get("status")
        takeable = status == "failed" or (
            status == "executing" and existing.get("started_at", 0) < stale_before
        )
        if not takeable:
            return False
        return self.set(key, new_record, ttl)

    # =========================================================================
    # Distributed Locking
    # =========================================================================

    @abstractmethod
    def get_lock(
        self,
        name: str,
        timeout: timedelta = timedelta(seconds=10),
        blocking_timeout: float | None = None,
    ) -> DistributedLock:
        """
        Get a distributed lock instance.

        Args:
            name: Lock name — the **post-prefix** portion of the storage
                key. The adapter prepends its own prefix via
                ``_make_key(name)`` before constructing the lock; the
                returned lock writes that fully resolved key verbatim.
                See ``DistributedLock`` Storage-key contract for the
                rationale and anti-pattern.
            timeout: Lock auto-release timeout (prevents deadlocks)
            blocking_timeout: Max time to wait when acquiring

        Returns:
            DistributedLock instance

        Example:
            >>> with cache.get_lock("circuit_breaker:payment") as lock:
            ...     # Critical section - only one process executes this
            ...     transition_circuit_breaker_state()

        Prefix semantics:
            ``cache.get_lock("foo")`` on a Redis adapter with default
            settings writes the Redis key ``baldur:foo`` (single prefix
            applied by ``_make_key``). Inside ``TestModeContext.start()``
            it shifts to ``xtest:baldur:foo`` (Redis-only).
            On Memcached / InMemory the configured static ``key_prefix``
            is honored without TestModeContext awareness.

            For SCAN observability of lock keys, callers should embed a
            ``lock:`` segment in the name (e.g.,
            ``cache.get_lock("idempotency:lock:order:abc")``).

        Note:
            Always use locks with context manager to ensure release.
            The timeout parameter prevents deadlocks if a process
            crashes while holding the lock.
        """
        pass

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    @abstractmethod
    def mget(self, keys: list[str]) -> dict[str, Any]:
        """
        Get multiple values at once.

        Args:
            keys: List of cache keys

        Returns:
            Dict mapping keys to values (missing keys omitted)
        """
        pass

    @abstractmethod
    def mset(
        self,
        mapping: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """
        Set multiple values at once.

        Args:
            mapping: Key-value pairs to set
            ttl: Optional TTL for all keys

        Returns:
            True if successful
        """
        pass

    def mdelete(self, keys: list[str]) -> int:
        """
        Delete multiple keys at once.

        Args:
            keys: List of cache keys to delete

        Returns:
            Number of keys that were deleted
        """
        deleted = 0
        for key in keys:
            if self.delete(key):
                deleted += 1
        return deleted

    # =========================================================================
    # Hash Operations (for structured data)
    # =========================================================================

    def hget(self, name: str, key: str) -> Any | None:
        """
        Get a field from a hash.

        Args:
            name: Hash name
            key: Field key within the hash

        Returns:
            Field value or None
        """
        hash_data = self.get(name)
        if isinstance(hash_data, dict):
            return hash_data.get(key)
        return None

    def hset(self, name: str, key: str, value: Any) -> bool:
        """
        Set a field in a hash.

        Args:
            name: Hash name
            key: Field key within the hash
            value: Field value

        Returns:
            True if successful
        """
        hash_data = self.get(name) or {}
        hash_data[key] = value
        return self.set(name, hash_data)

    def hgetall(self, name: str) -> dict[str, Any]:
        """
        Get all fields from a hash.

        Args:
            name: Hash name

        Returns:
            Dict of all fields and values
        """
        hash_data = self.get(name)
        return hash_data if isinstance(hash_data, dict) else {}

    # =========================================================================
    # List Operations (for bounded append + range queries)
    # =========================================================================

    def push_limit(
        self, key: str, value: Any, max_len: int, ttl: timedelta | None = None
    ) -> int:
        """
        Append value to a list and trim to max_len (oldest entries dropped).

        Default implementation uses RMW via get()/set(). Redis and InMemory
        adapters override with native atomic operations.

        Args:
            key: List key
            value: Value to append (must be serializable)
            max_len: Maximum list length (oldest entries trimmed)
            ttl: Time-to-live for the key (renewed on each call)

        Returns:
            Pre-trim length (length after push, before trim).
            > max_len means trim occurred.
        """
        current = self.get(key)
        if not isinstance(current, list):
            current = []
        current.append(value)
        pre_trim_len = len(current)
        if len(current) > max_len:
            current = current[-max_len:]
        self.set(key, current, ttl)
        return pre_trim_len

    def list_range(self, key: str, start: int, end: int) -> list[Any]:
        """
        Return elements from start to end (inclusive) of a list.

        Default implementation uses get() + slice. Redis and InMemory
        adapters override with native operations.

        Args:
            key: List key
            start: Start index (0-based, negative supported)
            end: End index (inclusive, -1 means last element)

        Returns:
            List of elements in the specified range
        """
        data = self.get(key)
        if not isinstance(data, list):
            return []
        if end == -1:
            return data[start:]
        return data[start : end + 1]

    # =========================================================================
    # Health Check
    # =========================================================================

    @abstractmethod
    def health_check(self) -> bool:
        """
        Check if cache backend is reachable.

        Returns:
            True if healthy and connected
        """
        pass

    @abstractmethod
    def flush_all(self) -> bool:
        """
        Clear all keys (USE WITH CAUTION - mainly for testing).

        Returns:
            True if successful

        Warning:
            This will delete ALL data in the cache. Only use
            in testing environments or with explicit confirmation.
        """
        pass

    def ping(self) -> bool:
        """
        Simple connectivity check.

        Returns:
            True if connection is alive
        """
        return self.health_check()

    # =========================================================================
    # Key Pattern Operations
    # =========================================================================

    def keys(self, pattern: str = "*") -> list[str]:
        """
        Find keys matching a pattern.

        Args:
            pattern: Glob-style pattern (e.g., "circuit_breaker:*")

        Returns:
            List of matching keys

        Warning:
            Use with caution in production - may be slow with many keys.
            Default implementation returns empty list.
        """
        return []

    def scan(
        self,
        pattern: str = "*",
        count: int = 100,
    ) -> tuple[int, list[str]]:
        """
        Incrementally iterate keys matching a pattern.

        Args:
            pattern: Glob-style pattern
            count: Approximate number of keys per iteration

        Returns:
            Tuple of (cursor, keys) - cursor 0 means scan complete

        Note:
            Default implementation returns (0, []).
            Override for implementations that support scanning.
        """
        return (0, [])


# ============================================================================
# Async Cache Provider Interface (idempotency dedup ops only)
# ============================================================================


class AsyncCacheProviderInterface(ABC):
    """Minimal awaitable cache surface for async idempotency dedup.

    Deliberately NOT a full async parity of :class:`CacheProviderInterface`.
    It exposes only the four operations the dedup gate awaits —
    ``asetnx`` / ``aget`` / ``acas_dict_field`` / ``adelete`` — so an adapter
    author implements just what an awaitable idempotency gate needs, instead of
    porting every sync method (get/set/incr/locks/…) to async. This mirrors the
    sync/async separation precedent (``AsyncResiliencePolicy`` vs
    ``ResiliencePolicy``); a full async cache is out of scope.

    Atomicity contract (identical to the sync surface):
        ``asetnx`` and ``acas_dict_field`` are the atomic dedup primitives.
        The base defaults here are non-atomic placeholders that production
        adapters MUST override with genuinely atomic implementations
        (Redis ``SET NX`` / Lua CAS). ``AsyncIdempotencyGate`` validates that an
        atomic override is in place via an identity check against these base
        methods (the analog of ``IdempotencyGate._validate_atomic_setnx`` /
        ``_validate_atomic_cas_dict_field``), so a subclass that inherits the
        default is rejected at gate construction (fail-closed) rather than
        silently double-executing.
    """

    @abstractmethod
    async def aget(self, key: str) -> Any | None:
        """Get value by key (awaitable).

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        ...

    @abstractmethod
    async def adelete(self, key: str) -> bool:
        """Delete key from cache (awaitable).

        Args:
            key: Cache key to delete

        Returns:
            True if key existed and was deleted
        """
        ...

    async def asetnx(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Set value only if the key does not exist (awaitable atomic acquire).

        Args:
            key: Cache key
            value: Value to set
            ttl: Optional time-to-live

        Returns:
            True if the key was set (did not exist), False otherwise.

        Note:
            The minimal async surface has no ``aset`` primitive, so — unlike the
            sync ``setnx`` — the base default cannot supply a working
            (non-atomic) implementation. It raises to make the "you must
            override with an atomic implementation" contract explicit. The gate's
            atomicity validator catches a non-overriding adapter by identity
            before this body is ever reached, so a correctly-wired setup never
            triggers the raise.
        """
        raise NotImplementedError(
            "AsyncCacheProviderInterface.asetnx has no atomic default; "
            f"{type(self).__name__} must override it with an atomic "
            "implementation (e.g. Redis SET NX)."
        )

    async def acas_dict_field(
        self,
        key: str,
        field: str,
        expected: Any,
        new_value: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomic compare-and-set on a single field of a dict record (awaitable).

        Reads the record at ``key``; if it is a dict whose ``field`` equals
        ``expected``, replaces the whole record with ``new_value`` (optional
        TTL). Otherwise returns False without writing.

        Args:
            key: Cache key holding a dict-valued record
            field: Field name within the dict to check
            expected: Expected current value of ``field``
            new_value: Replacement record (full dict, not a partial update)
            ttl: Optional time-to-live for the replacement record

        Returns:
            True if the record matched and was replaced, False otherwise.

        Note:
            Base default raises for the same reason as :meth:`asetnx` — the
            minimal surface has no read-modify-write primitive to build a
            non-atomic default from. Production adapters override with a
            single-round-trip atomic CAS (Redis Lua / lock-wrapped store).
        """
        raise NotImplementedError(
            "AsyncCacheProviderInterface.acas_dict_field has no atomic default; "
            f"{type(self).__name__} must override it with an atomic "
            "implementation (e.g. Redis Lua CAS)."
        )

    async def acas_takeover(
        self,
        key: str,
        new_record: dict[str, Any],
        *,
        stale_before: float,
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomically take over a failed / stale-executing dedup claim (awaitable).

        Awaitable twin of :meth:`CacheProviderInterface.cas_takeover`. Replaces
        the record at ``key`` with ``new_record`` iff the existing value is a
        dict whose ``status == "failed"`` OR (``status == "executing"`` AND
        ``started_at < stale_before``). Otherwise returns False without writing.

        Args:
            key: Cache key holding a dict-valued dedup record.
            new_record: Replacement record (full dict, not a partial update).
            stale_before: App-computed staleness threshold (an ``executing``
                record is takeable only if ``started_at < stale_before``).
            ttl: Optional time-to-live for the replacement record.

        Returns:
            True if the record was failed / stale-executing and was replaced,
            False otherwise.

        Note:
            Base default raises for the same reason as :meth:`asetnx` — the
            minimal surface has no read-modify-write primitive to build a
            non-atomic default from. Production adapters override with a
            single-round-trip atomic takeover (Redis Lua / lock-wrapped store).
        """
        raise NotImplementedError(
            "AsyncCacheProviderInterface.acas_takeover has no atomic default; "
            f"{type(self).__name__} must override it with an atomic "
            "implementation (e.g. Redis Lua CAS)."
        )
