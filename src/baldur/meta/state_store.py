"""
Meta-Watchdog state store.

Redis-backed store that keeps state across pod restarts.

Capabilities:
- consecutive_failures: per-component consecutive failure count
- last_loop_timestamp: timestamp of the last loop (for liveness)
- Distributed lock: prevents duplicate escalation across instances
  (the cross-worker dedup backend for EscalationManager.escalate())
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class WatchdogStateStore:
    """
    Watchdog state store.

    Persists the following state in Redis:
    - consecutive_failures: per-component consecutive failure count
    - last_loop_timestamp: timestamp of the last loop

    State survives a pod restart, so escalation can fire immediately after one.

    The escalation cooldown is owned by EscalationManager
    (per-process _last_escalation + the acquire_escalation_lock distributed
    lock).

    Example:
        store = WatchdogStateStore()

        # Increment the failure count
        count = store.increment_failure_count("dlq")

        # Prevent duplicate cross-worker escalation (SET NX EX)
        if store.acquire_escalation_lock("dlq", lock_ttl_seconds=3600):
            send_escalation()
    """

    # Redis key patterns
    KEY_PREFIX = "baldur:meta:watchdog"
    FAILURES_KEY = f"{KEY_PREFIX}:failures"  # Hash
    COOLDOWNS_KEY = f"{KEY_PREFIX}:cooldowns"  # Hash
    LAST_CHECK_KEY = f"{KEY_PREFIX}:last_check"  # String
    LAST_LOOP_KEY = f"{KEY_PREFIX}:last_loop_timestamp"  # String (liveness)

    # TTL (7 days - old data is cleaned up automatically)
    STATE_TTL_SECONDS = 7 * 24 * 60 * 60

    def __init__(self, redis_client: Any | None = None):
        """
        Initialize.

        Args:
            redis_client: Redis client (obtained automatically when None)
        """
        self._redis = redis_client
        self._local_failures: dict[str, int] = {}  # fallback storage
        self._local_cooldowns: dict[str, float] = {}
        self._local_last_loop: datetime | None = None
        self._lock = threading.RLock()

    def _get_redis(self) -> Any | None:
        """Obtain a Redis client.

        Uses the shared client from get_redis_client(), which applies TTL-based
        negative caching. This avoids blocking on a ~8s TCP timeout on every
        call while Redis is down.
        """
        if self._redis is not None:
            return self._redis

        try:
            from baldur.adapters.redis import get_redis_client

            client = get_redis_client()
            if client is not None:
                self._redis = client
            return client
        except ImportError:
            return None
        except Exception:
            return None

    # =========================================================================
    # Consecutive failures
    # =========================================================================

    def get_failure_count(self, component: str) -> int:
        """
        Read the consecutive failure count.

        Args:
            component: component name

        Returns:
            Consecutive failure count
        """
        redis = self._get_redis()
        if redis:
            try:
                count = redis.hget(self.FAILURES_KEY, component)
                if count:
                    if isinstance(count, bytes):
                        count = count.decode("utf-8")
                    return int(count)
            except Exception as e:
                logger.debug(
                    "watchdog_state_store.redis_get_failed",
                    error=e,
                )

        # Fallback: local memory
        with self._lock:
            return self._local_failures.get(component, 0)

    def increment_failure_count(self, component: str) -> int:
        """
        Increment the consecutive failure count.

        Args:
            component: component name

        Returns:
            The count after incrementing
        """
        redis = self._get_redis()
        if redis:
            try:
                new_count = redis.hincrby(self.FAILURES_KEY, component, 1)
                redis.expire(self.FAILURES_KEY, self.STATE_TTL_SECONDS)
                return new_count
            except Exception as e:
                logger.debug(
                    "watchdog_state_store.redis_incr_failed",
                    error=e,
                )

        # Fallback
        with self._lock:
            self._local_failures[component] = self._local_failures.get(component, 0) + 1
            return self._local_failures[component]

    def reset_failure_count(self, component: str) -> None:
        """
        Reset the consecutive failure count.

        Args:
            component: component name
        """
        redis = self._get_redis()
        if redis:
            try:
                redis.hdel(self.FAILURES_KEY, component)
            except Exception:
                pass

        with self._lock:
            self._local_failures.pop(component, None)

    def reset_all_failure_counts(self) -> None:
        """Reset every consecutive failure count."""
        redis = self._get_redis()
        if redis:
            try:
                redis.delete(self.FAILURES_KEY)
            except Exception:
                pass

        with self._lock:
            self._local_failures.clear()

    # =========================================================================
    # Last loop timestamp (liveness)
    # =========================================================================

    def update_last_loop_timestamp(self) -> None:
        """Refresh the last-loop timestamp."""
        now = utc_now()
        now_str = now.isoformat()

        redis = self._get_redis()
        if redis:
            try:
                redis.set(self.LAST_LOOP_KEY, now_str, ex=300)  # 5-minute TTL
            except Exception:
                pass

        with self._lock:
            self._local_last_loop = now

    def get_last_loop_timestamp(self) -> datetime | None:
        """
        Read the last-loop timestamp.

        Returns:
            Time of the last loop (None when unrecorded)
        """
        redis = self._get_redis()
        if redis:
            try:
                ts = redis.get(self.LAST_LOOP_KEY)
                if ts:
                    if isinstance(ts, bytes):
                        ts = ts.decode("utf-8")
                    return datetime.fromisoformat(ts)
            except Exception:
                pass

        with self._lock:
            return self._local_last_loop

    def get_last_loop_age_seconds(self) -> float:
        """
        Time elapsed since the last loop.

        Returns:
            Elapsed seconds, or infinity when unrecorded
        """
        last = self.get_last_loop_timestamp()
        if last is None:
            return float("inf")
        return (utc_now() - last).total_seconds()

    # =========================================================================
    # Distributed lock (duplicate escalation prevention)
    # =========================================================================

    def acquire_escalation_lock(
        self,
        component: str,
        lock_ttl_seconds: int = 30,
    ) -> bool:
        """
        Acquire the escalation lock.

        Guarantees that only one instance escalates a given component in a
        multi-instance deployment.

        Args:
            component: component name
            lock_ttl_seconds: lock TTL in seconds

        Returns:
            Whether the lock was acquired
        """
        redis = self._get_redis()
        if not redis:
            return True  # no Redis: proceed without a lock

        lock_key = f"{self.KEY_PREFIX}:escalation:lock:{component}"
        try:
            # SET NX EX: set only when the key is absent, with a TTL
            acquired = redis.set(lock_key, "1", nx=True, ex=lock_ttl_seconds)
            return bool(acquired)
        except Exception as e:
            logger.debug(
                "watchdog_state_store.lock_acquire_failed",
                error=e,
            )
            return True  # on failure, allow the caller to proceed

    def release_escalation_lock(self, component: str) -> None:
        """
        Release the escalation lock.

        Args:
            component: component name
        """
        redis = self._get_redis()
        if redis:
            lock_key = f"{self.KEY_PREFIX}:escalation:lock:{component}"
            try:
                redis.delete(lock_key)
            except Exception:
                pass

    # =========================================================================
    # Utilities
    # =========================================================================

    def clear_all(self) -> None:
        """Clear all state (for tests)."""
        redis = self._get_redis()
        if redis:
            try:
                redis.delete(self.FAILURES_KEY)
                redis.delete(self.COOLDOWNS_KEY)
                redis.delete(self.LAST_LOOP_KEY)
            except Exception:
                pass

        with self._lock:
            self._local_failures.clear()
            self._local_cooldowns.clear()
            self._local_last_loop = None


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_watchdog_state_store, configure_watchdog_state_store, reset_watchdog_state_store = (
    make_singleton_factory("watchdog_state_store", WatchdogStateStore)
)
