"""
Memcached Cache Adapter for the baldur system.

Implements CacheProviderInterface using Memcached as the backend.
Provides a lightweight, high-performance distributed cache alternative.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import structlog

from baldur.interfaces.cache_provider import (
    CacheProviderInterface,
    DistributedLock,
    generate_lock_owner_id,
)
from baldur.utils.serialization import fast_dumps, fast_loads

logger = structlog.get_logger()


class MemcachedDistributedLock(DistributedLock):
    """
    Memcached-based distributed lock implementation.

    Uses Memcached ADD operation for atomic lock acquisition.
    Note: This is a best-effort lock - Memcached doesn't support true distributed locks.
    For critical sections, consider using Redis instead.
    """

    def __init__(
        self,
        client,
        full_key: str,
        timeout: timedelta = timedelta(seconds=10),
        blocking_timeout: float | None = None,
    ):
        """
        Initialize the lock.

        Args:
            client: Memcached client instance
            full_key: Verbatim Memcached key (caller-resolved, written
                as-is). ``MemcachedCacheAdapter.get_lock()`` resolves
                ``self._make_key(name)`` once and passes the result here;
                this lock applies no further transformation (no extra
                prefix segment, no truncation — those are the adapter's
                responsibility before this constructor runs).
            timeout: Lock auto-release timeout
            blocking_timeout: Max time to wait when acquiring
        """
        self._client = client
        self._name = full_key
        self._timeout = int(timeout.total_seconds())
        self._blocking_timeout = blocking_timeout
        self._token = generate_lock_owner_id()
        self._acquired = False

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """
        Acquire the lock using Memcached ADD.

        Args:
            blocking: If True, block until lock acquired
            timeout: Override blocking timeout

        Returns:
            True if lock acquired, False otherwise
        """
        blocking_timeout = timeout if timeout is not None else self._blocking_timeout

        if not blocking:
            return self._try_acquire()

        start = time.time()
        while True:
            if self._try_acquire():
                return True

            if blocking_timeout is not None:
                elapsed = time.time() - start
                if elapsed >= blocking_timeout:
                    return False

            time.sleep(0.1)

    def _try_acquire(self) -> bool:
        """Attempt to acquire the lock once."""
        try:
            # ADD only succeeds if key doesn't exist
            result = self._client.add(
                self._name,
                self._token,
                expire=self._timeout,
            )
            if result:
                self._acquired = True
                return True
            return False
        except Exception as e:
            logger.exception(
                "memcached_lock.acquire_lock_failed",
                error=e,
            )
            return False

    def release(self) -> None:
        """Release the lock if we own it."""
        if not self._acquired:
            return

        try:
            # Verify we still own the lock
            current = self._client.get(self._name)
            if current == self._token:
                self._client.delete(self._name)
            self._acquired = False
        except Exception as e:
            logger.warning(
                "memcached_lock.release_lock_failed",
                error=e,
            )

    def locked(self) -> bool:
        """Check if lock is currently held."""
        try:
            return self._client.get(self._name) is not None
        except Exception:
            return False

    def owned(self) -> bool:
        """Check if lock is held by this instance."""
        if not self._acquired:
            return False
        try:
            return self._client.get(self._name) == self._token
        except Exception:
            return False


class MemcachedCacheAdapter(CacheProviderInterface):
    """
    Memcached implementation of CacheProviderInterface.

    Uses pymemcache for efficient binary protocol communication.
    Supports JSON serialization for complex objects.

    Requirements:
        - pymemcache

    Configuration:
        - MEMCACHED_SERVERS: List of server addresses (default: ["localhost:11211"])

    Limitations compared to Redis:
        - No atomic increment on non-existent keys (creates with 0 first)
        - Limited lock support (best-effort, not guaranteed)
        - No native TTL query
        - Values must fit in 1MB (default slab size)

    Usage:
        adapter = MemcachedCacheAdapter(servers=["localhost:11211"])
        adapter.set("key", {"data": "value"}, ttl=timedelta(hours=1))
        value = adapter.get("key")
    """

    def __init__(
        self,
        servers: list[str] | None = None,
        key_prefix: str = "baldur:",
        connect_timeout: float = 5.0,
        timeout: float = 5.0,
    ):
        """
        Initialize the Memcached cache adapter.

        Args:
            servers: List of server addresses (host:port). If None, reads from settings.
            key_prefix: Prefix for all cache keys (default: "baldur:")
            connect_timeout: Connection timeout in seconds.
            timeout: Read/write timeout in seconds.
        """
        self._servers = servers
        self._key_prefix = key_prefix
        self._connect_timeout = connect_timeout
        self._timeout = timeout
        self._client = None
        self._pymemcache = None

    @property
    def pymemcache(self):
        """Get pymemcache module."""
        if self._pymemcache is None:
            try:
                import pymemcache

                self._pymemcache = pymemcache
            except ImportError as _err:
                raise ImportError(
                    "pymemcache is required for MemcachedCacheAdapter. "
                    "Install it with: pip install pymemcache"
                ) from _err
        return self._pymemcache

    @property
    def client(self):
        """Get Memcached client, creating one if needed."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def _create_client(self):
        """Create a Memcached client."""
        servers = self._servers

        if servers is None:
            # Try to get from Django settings
            try:
                from django.conf import settings

                servers = getattr(settings, "MEMCACHED_SERVERS", None)
            except ImportError:
                pass

        if servers is None:
            # Try environment variable
            import os

            env_servers = os.environ.get("MEMCACHED_SERVERS", "")
            if env_servers:
                servers = [s.strip() for s in env_servers.split(",")]

        if servers is None:
            servers = ["localhost:11211"]

        # Parse server addresses
        parsed_servers = []
        for server in servers:
            if ":" in server:
                host, port = server.split(":")
                parsed_servers.append((host, int(port)))
            else:
                parsed_servers.append((server, 11211))

        from pymemcache.client.hash import HashClient

        # Use JSON serializer for complex objects
        return HashClient(
            servers=parsed_servers,
            connect_timeout=self._connect_timeout,
            timeout=self._timeout,
            serializer=self._json_serializer,
            deserializer=self._json_deserializer,
            use_pooling=True,
            max_pool_size=10,
        )

    def _json_serializer(self, key: str, value: Any) -> tuple[bytes, int]:
        """Serialize value to JSON bytes."""
        if isinstance(value, bytes):
            return value, 1
        return fast_dumps(value), 2

    def _json_deserializer(self, key: str, value: bytes, flags: int) -> Any:
        """Deserialize value from JSON bytes."""
        if flags == 1:
            return value
        if flags == 2:
            return fast_loads(value)
        return value.decode("utf-8")

    def _make_key(self, key: str) -> str:
        """Add prefix to key and ensure it's valid for Memcached."""
        full_key = f"{self._key_prefix}{key}"
        # Memcached keys can't contain whitespace or control characters
        # and must be <= 250 bytes
        return full_key.replace(" ", "_")[:250]

    @property
    def provider_name(self) -> str:
        """Return the provider name."""
        return "memcached"

    # =========================================================================
    # Basic Operations
    # =========================================================================

    def get(self, key: str) -> Any | None:
        """Get value by key."""
        try:
            return self.client.get(self._make_key(key))
        except Exception as e:
            logger.exception(
                "memcached_cache.get_failed_key",
                cache_key=key,
                error=e,
            )
            return None

    def set(
        self,
        key: str,
        value: Any,
        ttl: timedelta | None = None,
    ) -> bool:
        """Set value with optional TTL."""
        try:
            expire = int(ttl.total_seconds()) if ttl else 0
            return self.client.set(self._make_key(key), value, expire=expire)
        except Exception as e:
            logger.exception(
                "memcached_cache.set_failed_key",
                cache_key=key,
                error=e,
            )
            return False

    def delete(self, key: str) -> bool:
        """Delete key from cache."""
        try:
            return self.client.delete(self._make_key(key))
        except Exception as e:
            logger.exception(
                "memcached_cache.delete_failed_key",
                cache_key=key,
                error=e,
            )
            return False

    def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        try:
            return self.client.get(self._make_key(key)) is not None
        except Exception as e:
            logger.exception(
                "memcached_cache.exists_check_failed_key",
                cache_key=key,
                error=e,
            )
            return False

    # =========================================================================
    # Atomic Operations
    # =========================================================================

    def incr(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment a counter.

        Note: Memcached requires the key to exist for incr/decr.
        This implementation creates the key if it doesn't exist.
        """
        try:
            full_key = self._make_key(key)

            # Try to increment
            try:
                result = self.client.incr(full_key, amount)
                if result is not None:
                    return result
            except Exception:
                pass

            # Key doesn't exist, create it
            self.client.set(full_key, amount, expire=0)
            return amount

        except Exception as e:
            logger.exception(
                "memcached_cache.incr_failed_key",
                cache_key=key,
                error=e,
            )
            return 0

    def decr(self, key: str, amount: int = 1) -> int:
        """
        Atomically decrement a counter.

        Note: Memcached decr cannot go below 0.
        """
        try:
            full_key = self._make_key(key)

            try:
                result = self.client.decr(full_key, amount)
                if result is not None:
                    return result
            except Exception:
                pass

            # Key doesn't exist, return 0
            return 0

        except Exception as e:
            logger.exception(
                "memcached_cache.decr_failed_key",
                cache_key=key,
                error=e,
            )
            return 0

    def expire(self, key: str, ttl: timedelta) -> bool:
        """
        Set expiration on existing key.

        Note: Memcached doesn't support changing TTL directly.
        This implementation does a get/set cycle.
        """
        try:
            full_key = self._make_key(key)
            value = self.client.get(full_key)
            if value is None:
                return False
            return self.client.set(full_key, value, expire=int(ttl.total_seconds()))
        except Exception as e:
            logger.exception(
                "memcached_cache.expire_failed_key",
                cache_key=key,
                error=e,
            )
            return False

    def ttl(self, key: str) -> int | None:
        """
        Get remaining TTL in seconds.

        Note: Memcached doesn't expose TTL. This returns None always.
        For TTL tracking, consider using Redis instead.
        """
        # Memcached doesn't support TTL queries
        # Return None to indicate unknown
        if not self.exists(key):
            return -2
        return None

    # =========================================================================
    # Distributed Locking
    # =========================================================================

    def get_lock(
        self,
        name: str,
        timeout: timedelta = timedelta(seconds=10),
        blocking_timeout: float | None = None,
    ) -> DistributedLock:
        """
        Get a distributed lock instance.

        Note: Memcached locks are best-effort. For guaranteed locks, use Redis.

        Args:
            name: Lock name (should be unique across application)
            timeout: Lock auto-release timeout
            blocking_timeout: Max time to wait when acquiring

        Returns:
            DistributedLock instance
        """
        return MemcachedDistributedLock(
            client=self.client,
            full_key=self._make_key(name),
            timeout=timeout,
            blocking_timeout=blocking_timeout,
        )

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    def mget(self, keys: list[str]) -> dict[str, Any]:
        """Get multiple values at once."""
        try:
            full_keys = [self._make_key(k) for k in keys]
            result = self.client.get_many(full_keys)

            # Map back to original keys
            prefix_len = len(self._key_prefix)
            return {k[prefix_len:]: v for k, v in result.items() if v is not None}
        except Exception as e:
            logger.exception(
                "memcached_cache.mget_failed",
                error=e,
            )
            return {}

    def mset(
        self,
        mapping: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """Set multiple values at once."""
        try:
            expire = int(ttl.total_seconds()) if ttl else 0
            full_mapping = {self._make_key(k): v for k, v in mapping.items()}

            failed = self.client.set_many(full_mapping, expire=expire)
            return len(failed) == 0

        except Exception as e:
            logger.exception(
                "memcached_cache.mset_failed",
                error=e,
            )
            return False

    # =========================================================================
    # Health Check
    # =========================================================================

    def health_check(self) -> bool:
        """Check if Memcached is reachable."""
        try:
            # Set and get a test key
            test_key = self._make_key("__health_check__")
            self.client.set(test_key, "ok", expire=10)
            result = self.client.get(test_key)
            return result == "ok"
        except Exception as e:
            logger.exception(
                "memcached_cache.health_check_failed",
                error=e,
            )
            return False

    def flush_all(self) -> bool:
        """
        Clear all keys (USE WITH CAUTION - mainly for testing).

        Note: This flushes ALL keys in Memcached, not just our prefixed keys.
        """
        try:
            self.client.flush_all()
            logger.warning("memcached_cache.flushed_all_keys")
            return True
        except Exception as e:
            logger.exception(
                "memcached_cache.flush_failed",
                error=e,
            )
            return False

    # =========================================================================
    # Memcached-specific Methods
    # =========================================================================

    def stats(self) -> dict[str, Any]:
        """
        Get Memcached server statistics.

        Returns:
            Dictionary of server stats
        """
        try:
            return self.client.stats()
        except Exception as e:
            logger.exception(
                "memcached_cache.stats_failed",
                error=e,
            )
            return {}

    def touch(self, key: str, ttl: timedelta) -> bool:
        """
        Update TTL without changing value.

        Note: Requires Memcached 1.4.8+
        """
        try:
            return self.client.touch(
                self._make_key(key), expire=int(ttl.total_seconds())
            )
        except Exception as e:
            logger.exception(
                "memcached_cache.touch_failed_key",
                cache_key=key,
                error=e,
            )
            return False
