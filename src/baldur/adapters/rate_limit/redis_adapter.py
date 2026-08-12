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

import math
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

# Seconds of headroom added on top of a cooldown's remaining time when deriving
# the Redis key TTL, so the key cannot be evicted in the instant before the
# cooldown it carries expires. Not an operator tuning axis — the quantity is a
# rounding cushion, not a policy.
_COOLDOWN_TTL_MARGIN_SECONDS = 60

_EXTEND_COOLDOWN_SCRIPT = "rate_limit_extend_cooldown"

# Monotonic cooldown write, atomic across processes and hosts.
#
# Single KEY on purpose: the shared LuaScriptRegistry rejects multi-key calls
# whose keys land in different hash slots, and on Redis Cluster a second key
# named through ARGV is a non-local key the server refuses outright. The
# last_updated bookkeeping therefore stays outside the script.
#
# The TTL is derived from the *effective* expiry, never from the caller's
# candidate: a short headerless 429 arriving against a long honored cooldown
# would otherwise shrink the key's TTL below the expiry it carries and let Redis
# drop a live cooldown early. Only the script knows the effective value.
LUA_EXTEND_COOLDOWN = """
local stored = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
local candidate = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local configured_ttl = tonumber(ARGV[3])
local margin = tonumber(ARGV[4])

local effective = stored
if candidate > effective then
    effective = candidate
end

local ttl = math.ceil(effective - now) + margin
if ttl < configured_ttl then
    ttl = configured_ttl
end
if ttl < 1 then
    ttl = 1
end

redis.call('SET', KEYS[1], string.format('%.6f', effective), 'EX', string.format('%d', ttl))
return string.format('%.6f', effective)
"""


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
        from baldur.audit.performance.lua_registry import LuaScriptRegistry

        self._redis = redis_client
        self._ttl = ttl if ttl is not None else _get_redis_ttl()
        self._available: bool | None = None
        # v6.3.0: Fallback-mode and local-state tracking
        self._fallback_mode = False
        self._local_state: dict[str, RateLimitState] = {}  # Local fallback state
        self._lua = LuaScriptRegistry(redis_client)
        self._lua.register(_EXTEND_COOLDOWN_SCRIPT, LUA_EXTEND_COOLDOWN)
        # Sticky once a reachable backend has proven it cannot run Lua (a client
        # without scripting support, an ACL denying @scripting, an EVAL-rejecting
        # proxy). Deliberately unlocked and never reset: the worst race is one
        # extra WARNING per thread already inside the window, once per process.
        self._script_fallback = False

    @property
    def storage_type(self) -> RateLimitStorageType:
        return RateLimitStorageType.REDIS

    def _make_key(self, key: str, suffix: str) -> str:
        """Generate Redis key with prefix."""
        return f"{self.KEY_PREFIX}:{key}:{suffix}"

    def is_available(self) -> bool:
        """Check if Redis is available.

        The unavailable edge is announced at WARNING, except when nobody
        configured Redis outside production: this adapter is only
        auto-constructed against the shipped default URL in that posture, so
        the failure is the framework finding its own default unreachable.
        """
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
                from baldur.settings.redis import redis_absence_is_expected

                record_ratelimit_redis_unavailable()
                if redis_absence_is_expected():
                    logger.debug(
                        "redis_rate_limit_storage.redis_unavailable",
                        error=e,
                    )
                else:
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

    def _read_state(self, key: str) -> RateLimitState:
        """Read the three state keys in one round trip. Lets Redis errors escape."""
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

    def get_state(self, key: str) -> RateLimitState:
        """Get rate limit state from Redis."""
        try:
            return self._read_state(key)

        except Exception as e:
            logger.exception(
                "redis_rate_limit_storage.get_state_failed",
                error=e,
            )
            return RateLimitState(key=key)

    def get_state_strict(self, key: str) -> RateLimitState:
        """Get rate limit state from Redis, raising instead of folding on failure."""
        try:
            return self._read_state(key)

        except Exception as e:
            logger.exception(
                "redis_rate_limit_storage.get_state_strict_failed",
                error=e,
            )
            raise RateLimitStorageUnavailableError(str(e)) from e

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

    def extend_cooldown(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None = None,
    ) -> float:
        """Move the cooldown end time later in one atomic server-side max-merge.

        Falls back to a read-modify-write when this Redis cannot run Lua. That
        fallback is unsynchronised — it is monotonic with respect to what it
        read, so a longer write landing during its round trip is lost, whether
        that writer is another thread here or another host. It is still the only
        acceptable degradation: raising instead would reach every caller's
        fail-open wrap and install no cooldown at all — the worst outcome
        available, on exactly the storm the cooldown exists to damp.
        """
        if self._script_fallback:
            return self._extend_cooldown_unscripted(key, cooldown_until, ttl)

        try:
            return self._extend_cooldown_scripted(key, cooldown_until, ttl)
        except Exception as script_error:
            # The plain-command path decides what this failure was: if it also
            # fails the backend is down and the error propagates, if it succeeds
            # the backend is reachable and scripting specifically is unusable.
            effective = self._extend_cooldown_unscripted(key, cooldown_until, ttl)
            self._script_fallback = True
            logger.warning(
                "redis_rate_limit_storage.script_fallback",
                redis_key=key,
                error=script_error,
            )
            return effective

    def _extend_cooldown_scripted(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None,
    ) -> float:
        """Atomic max-merge through the shared Lua registry."""
        now = time.time()
        effective = float(
            self._lua.execute(
                _EXTEND_COOLDOWN_SCRIPT,
                keys=[self._make_key(key, "cooldown_until")],
                args=[
                    repr(cooldown_until),
                    repr(now),
                    ttl or self._ttl,
                    _COOLDOWN_TTL_MARGIN_SECONDS,
                ],
            )
        )
        self._touch_last_updated(key, now, self._covering_ttl(effective, now, ttl))
        return effective

    def _extend_cooldown_unscripted(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None,
    ) -> float:
        """Read-modify-write max-merge, for a Redis that cannot run Lua.

        Reads strictly: a folded read would report no stored cooldown and let a
        short candidate replace a live long one, which is the very regression
        the monotonic contract exists to close.
        """
        now = time.time()
        stored = self.get_state_strict(key).cooldown_until
        effective = max(stored, cooldown_until)
        covering_ttl = self._covering_ttl(effective, now, ttl)

        try:
            pipeline = self._redis.pipeline()
            pipeline.set(
                self._make_key(key, "cooldown_until"),
                str(effective),
                ex=covering_ttl,
            )
            pipeline.set(
                self._make_key(key, "last_updated"),
                str(now),
                ex=covering_ttl,
            )
            pipeline.execute()
        except Exception as e:
            logger.exception(
                "redis_rate_limit_storage.extend_cooldown_failed",
                error=e,
            )
            raise RateLimitStorageUnavailableError(str(e)) from e

        return effective

    def _touch_last_updated(self, key: str, now: float, ttl: int) -> None:
        """Write the last_updated marker beside the cooldown (best-effort).

        Kept out of the Lua script because a second key would make the call
        multi-key, which the shared registry rejects on standalone Redis and
        Redis Cluster refuses outright. Nothing branches on this value, so an
        unordered write beside the atomic one costs nothing.
        """
        try:
            self._redis.set(self._make_key(key, "last_updated"), str(now), ex=ttl)
        except Exception as e:
            logger.debug(
                "redis_rate_limit_storage.last_updated_write_skipped",
                redis_key=key,
                error=e,
            )

    def _covering_ttl(self, effective: float, now: float, ttl: int | None) -> int:
        """TTL that outlives the effective expiry it is applied to."""
        configured = ttl or self._ttl
        return max(
            configured,
            math.ceil(effective - now) + _COOLDOWN_TTL_MARGIN_SECONDS,
        )

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
