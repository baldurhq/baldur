"""
Redis Rate Limit Storage Adapter

High-performance distributed rate limit storage using Redis.
Provides atomic operations for multi-server environments.

Requirements:
    - redis>=4.0.0

Features:
    - Atomic increment/set operations
    - Automatic TTL-based cleanup
    - Fastest option for distributed rate limiting
    - v6.3.0: Drift detection and fallback metrics
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from baldur.interfaces.rate_limit_storage import (
    RateLimitState,
    RateLimitStorageInterface,
    RateLimitStorageType,
    RateLimitStorageUnavailableError,
)

# Drift detection metrics
try:
    from baldur.metrics.drift_metrics import (
        record_ratelimit_drift,
        record_ratelimit_reconciliation,
        record_ratelimit_redis_unavailable,
        set_ratelimit_fallback_mode,
    )

    HAS_DRIFT_METRICS = True
except ImportError:
    HAS_DRIFT_METRICS = False

    def record_ratelimit_redis_unavailable() -> None:
        return None

    def record_ratelimit_drift(key: str) -> None:
        return None

    def set_ratelimit_fallback_mode(active: bool) -> None:
        return None

    def record_ratelimit_reconciliation(success: bool) -> None:
        return None


logger = structlog.get_logger()


def _get_redis_ttl() -> int:
    """Read the Redis TTL from RateLimitSettings."""
    try:
        from baldur.settings.rate_limit import get_rate_limit_settings

        return get_rate_limit_settings().redis_ttl
    except Exception:
        return 3600  # 1 hour fallback


class RedisRateLimitStorage(RateLimitStorageInterface):
    """
    Redis-based rate limit storage.

    Uses Redis for atomic distributed rate limit state management.
    Recommended for production multi-server environments.

    v6.3.0: Drift detection
    - Fallback-mode tracking and metrics
    - Sync with local state once Redis recovers
    - Drift detection and reconciliation

    Key schema:
        ratelimit:{key}:cooldown_until - float timestamp
        ratelimit:{key}:consecutive_429s - int counter
        ratelimit:{key}:last_updated - float timestamp

    Example:
        redis_client = redis.Redis(host='localhost', port=6379, db=0)
        storage = RedisRateLimitStorage(redis_client)

        # Set cooldown after 429
        storage.set_cooldown("payment_api", time.time() + 60)
    """

    KEY_PREFIX = "ratelimit"
    DEFAULT_TTL = 3600  # Legacy constant kept for backward compatibility

    def __init__(self, redis_client: Any, ttl: int | None = None) -> None:
        """
        Initialize Redis rate limit storage.

        Args:
            redis_client: Redis client instance (redis.Redis or compatible)
            ttl: Redis key TTL (seconds). Taken from settings when None.
        """
        self._redis = redis_client
        self._ttl = ttl if ttl is not None else _get_redis_ttl()
        self._available: bool | None = None
        # v6.3.0: Fallback-mode and local-state tracking
        self._fallback_mode = False
        self._local_state: dict[str, RateLimitState] = {}  # Local fallback state

    @property
    def storage_type(self) -> RateLimitStorageType:
        return RateLimitStorageType.REDIS

    def _make_key(self, key: str, suffix: str) -> str:
        """Generate Redis key with prefix."""
        return f"{self.KEY_PREFIX}:{key}:{suffix}"

    def is_available(self) -> bool:
        """Check if Redis is available."""
        try:
            self._redis.ping()
            # v6.3.0: Recovered - a drift check is required
            if self._fallback_mode:
                self._reconcile_after_recovery()
            self._fallback_mode = False
            set_ratelimit_fallback_mode(False)
            self._available = True
            return True
        except Exception as e:
            # v6.3.0: Record the Redis-unavailable metric
            if not self._fallback_mode:
                record_ratelimit_redis_unavailable()
                logger.warning(
                    "redis_rate_limit_storage.redis_unavailable",
                    error=e,
                )
            self._fallback_mode = True
            set_ratelimit_fallback_mode(True)
            self._available = False
            return False

    def _reconcile_after_recovery(self) -> None:
        """Sync with local state after Redis recovers."""
        if not self._local_state:
            return

        for key, local_state in list(self._local_state.items()):
            try:
                redis_state = self._get_state_from_redis(key)
                # Compare local state against Redis state
                if redis_state is not None and (
                    local_state.cooldown_until != redis_state.cooldown_until
                    or local_state.consecutive_429s != redis_state.consecutive_429s
                ):
                    record_ratelimit_drift(key)
                    logger.info(
                        "redis_rate_limit_storage.drift_detected_syncing_local",
                        redis_key=key,
                    )
                    # Choose the more conservative value (safety first)
                    merged = self._merge_conservative(local_state, redis_state)
                    self._save_to_redis(key, merged)
                    record_ratelimit_reconciliation(success=True)
            except Exception as e:
                logger.warning(
                    "redis_rate_limit_storage.reconciliation_failed",
                    redis_key=key,
                    error=e,
                )
                record_ratelimit_reconciliation(success=False)

        self._local_state.clear()

    def _get_state_from_redis(self, key: str) -> RateLimitState | None:
        """Read state directly from Redis (internal use)."""
        try:
            pipeline = self._redis.pipeline()
            pipeline.get(self._make_key(key, "cooldown_until"))
            pipeline.get(self._make_key(key, "consecutive_429s"))
            pipeline.get(self._make_key(key, "last_updated"))
            results = pipeline.execute()

            return RateLimitState(
                key=key,
                cooldown_until=float(results[0]) if results[0] else 0.0,
                consecutive_429s=int(results[1]) if results[1] else 0,
                last_updated=float(results[2]) if results[2] else 0.0,
            )
        except Exception:
            return None

    def _merge_conservative(
        self,
        local: RateLimitState,
        remote: RateLimitState,
    ) -> RateLimitState:
        """Pick the more conservative of the two states."""
        return RateLimitState(
            key=local.key,
            # Take the longer cooldown (safety first)
            cooldown_until=max(local.cooldown_until, remote.cooldown_until),
            # Take the higher 429 count
            consecutive_429s=max(local.consecutive_429s, remote.consecutive_429s),
            # Take the more recent timestamp
            last_updated=max(local.last_updated, remote.last_updated),
        )

    def _save_to_redis(self, key: str, state: RateLimitState) -> None:
        """Save state to Redis (internal use)."""
        pipeline = self._redis.pipeline()
        pipeline.set(
            self._make_key(key, "cooldown_until"),
            str(state.cooldown_until),
            ex=self._ttl,
        )
        pipeline.set(
            self._make_key(key, "consecutive_429s"),
            str(state.consecutive_429s),
            ex=self._ttl,
        )
        pipeline.set(
            self._make_key(key, "last_updated"),
            str(state.last_updated),
            ex=self._ttl,
        )
        pipeline.execute()

    def get_state(self, key: str) -> RateLimitState:
        """Get rate limit state from Redis."""
        try:
            pipeline = self._redis.pipeline()
            pipeline.get(self._make_key(key, "cooldown_until"))
            pipeline.get(self._make_key(key, "consecutive_429s"))
            pipeline.get(self._make_key(key, "last_updated"))

            results = pipeline.execute()

            cooldown_until = float(results[0]) if results[0] else 0.0
            consecutive_429s = int(results[1]) if results[1] else 0
            last_updated = float(results[2]) if results[2] else 0.0

            return RateLimitState(
                key=key,
                cooldown_until=cooldown_until,
                consecutive_429s=consecutive_429s,
                last_updated=last_updated,
            )

        except Exception as e:
            logger.exception(
                "redis_rate_limit_storage.get_state_failed",
                error=e,
            )
            return RateLimitState(key=key)

    def set_cooldown(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None = None,
    ) -> None:
        """Set cooldown in Redis with TTL."""
        try:
            ttl = ttl or self._ttl
            now = time.time()

            pipeline = self._redis.pipeline()
            pipeline.set(
                self._make_key(key, "cooldown_until"),
                str(cooldown_until),
                ex=ttl,
            )
            pipeline.set(
                self._make_key(key, "last_updated"),
                str(now),
                ex=ttl,
            )
            pipeline.execute()

            logger.debug(
                "redis_rate_limit_storage.set_cooldown",
                redis_key=key,
                cooldown_until=cooldown_until,
                ttl=ttl,
            )

        except Exception as e:
            logger.exception(
                "redis_rate_limit_storage.set_cooldown_failed",
                error=e,
            )
            raise RateLimitStorageUnavailableError(str(e)) from e

    def increment_consecutive_429s(self, key: str) -> int:
        """Atomically increment 429 counter in Redis."""
        try:
            redis_key = self._make_key(key, "consecutive_429s")

            # Atomic increment with TTL
            pipeline = self._redis.pipeline()
            pipeline.incr(redis_key)
            pipeline.expire(redis_key, self._ttl)
            results = pipeline.execute()

            new_value = results[0]
            logger.debug(
                "redis_rate_limit_storage.incremented_counter",
                redis_key=key,
                new_value=new_value,
            )
            return new_value

        except Exception as e:
            logger.exception(
                "redis_rate_limit_storage.increment_failed",
                error=e,
            )
            raise RateLimitStorageUnavailableError(str(e)) from e

    def reset_consecutive_429s(self, key: str) -> None:
        """Reset 429 counter in Redis."""
        try:
            self._redis.delete(self._make_key(key, "consecutive_429s"))
            logger.debug(
                "redis_rate_limit_storage.reset_counter",
                redis_key=key,
            )

        except Exception as e:
            logger.exception(
                "redis_rate_limit_storage.reset_failed",
                error=e,
            )

    def clear(self, key: str) -> None:
        """Clear all rate limit state for a key."""
        try:
            pipeline = self._redis.pipeline()
            pipeline.delete(self._make_key(key, "cooldown_until"))
            pipeline.delete(self._make_key(key, "consecutive_429s"))
            pipeline.delete(self._make_key(key, "last_updated"))
            pipeline.execute()

            logger.debug(
                "redis_rate_limit_storage.cleared_state",
                redis_key=key,
            )

        except Exception as e:
            logger.exception(
                "redis_rate_limit_storage.clear_failed",
                error=e,
            )
