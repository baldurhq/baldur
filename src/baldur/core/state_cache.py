# packages/baldur-python/src/baldur/core/state_cache.py
"""
Circuit Breaker state cache (Platinum SLA optimization).

TTL-based local caching minimizes network calls to the command center;
polling jitter plus singleflight miss deduplication prevent the two
thundering-herd classes (synchronized expiry across processes,
concurrent misses within one process).

Settings are overridable via environment variables through
StateCacheSettings:
- BALDUR_STATE_CACHE_BASE_TTL
- BALDUR_STATE_CACHE_JITTER_RANGE
"""

import random
import time
from collections.abc import Callable
from typing import Any

from baldur.core.ttl_cache import TTLCacheBase
from baldur.settings.state_cache import get_state_cache_settings

__all__ = ["CBStateCache"]


class CBStateCache:
    """
    Circuit Breaker state cache.

    - TTL-based local caching minimizes network calls
    - Polling jitter desynchronizes expiry across processes
    - Concurrent same-service misses are deduplicated to one fetch
      (TTLCacheBase.get_or_compute singleflight)

    Usage:
        def fetch_cb_state(service):
            return requests.get(f'http://command-center/cb/{service}').json()

        CBStateCache.configure(fetch_callback=fetch_cb_state)
        state = CBStateCache.get_state('payment')
    """

    # Delegate: TTL/expiry/thread-safety handled by TTLCacheBase.
    # TTL is always passed via ttl_override so the base default is unused.
    _delegate: TTLCacheBase[str, dict[str, Any]] = TTLCacheBase(ttl_seconds=30.0)
    _fetch_callback: Callable[[str], dict] | None = None

    @classmethod
    def _get_base_ttl(cls) -> float:
        """Base TTL (seconds). Loaded from StateCacheSettings."""
        return get_state_cache_settings().base_ttl

    @classmethod
    def _get_jitter_range(cls) -> float:
        """Random jitter range (seconds). Loaded from StateCacheSettings."""
        return get_state_cache_settings().jitter_range

    @classmethod
    def configure(cls, fetch_callback: Callable[[str], dict]) -> None:
        """
        Configure the state-fetch callback.

        Args:
            fetch_callback: Function that takes a service name and
                fetches its CB state from the command center
        """
        cls._fetch_callback = fetch_callback

    @classmethod
    def get_state(cls, service: str) -> dict | None:
        """
        Look up CB state (cache first).

        Cache hit: ~0.01ms
        Cache miss: one command-center call shared by all concurrent
        callers for the same service (singleflight)

        On fetch failure, enters degraded mode and returns the degraded
        config; nothing is cached so the next call retries the fetch.

        Args:
            service: Service identifier

        Returns:
            CB state dictionary or None
        """
        state = cls._delegate.get(service)
        if state is not None:
            return state

        def _fetch() -> dict[str, Any] | None:
            if not cls._fetch_callback:
                return None  # not cached - get_or_compute's None sentinel
            return cls._fetch_callback(service)

        try:
            # Cache miss: dedup concurrent fetches for the same service.
            # _calculate_ttl() (settings read + random.uniform) is
            # evaluated only on this miss path, as before.
            return cls._delegate.get_or_compute(
                service, _fetch, ttl_override=cls._calculate_ttl()
            )
        except Exception:
            # Command center unreachable -> degraded mode. Waiters share
            # the winner's exception, so each takes this same path:
            # idempotent, one fetch-timeout total instead of N. The
            # degraded config is NOT cached.
            from baldur.core.degraded_mode_handler import DegradedModeHandler

            DegradedModeHandler.enter_degraded_mode(reason="command_center_unreachable")
            return DegradedModeHandler.get_cb_config()

    @classmethod
    def set_state(cls, service: str, state: dict[str, Any]) -> None:
        """
        Store CB state directly in the cache (for tests or manual updates).

        Args:
            service: Service identifier
            state: CB state dictionary
        """
        ttl = cls._calculate_ttl()
        cls._delegate.set(service, state, ttl_override=ttl)

    @classmethod
    def invalidate(cls, service: str) -> None:
        """Invalidate the cache entry for a specific service."""
        cls._delegate.invalidate(service)

    @classmethod
    def invalidate_all(cls) -> None:
        """Invalidate the entire cache."""
        cls._delegate.invalidate_all()

    @classmethod
    def get_cache_stats(cls) -> dict[str, Any]:
        """Cache statistics snapshot."""
        with cls._delegate._lock:
            now = time.time()
            total = len(cls._delegate._cache)
            expired = sum(
                1 for e in cls._delegate._cache.values() if now >= e.expires_at
            )
            return {
                "total_entries": total,
                "expired_entries": expired,
                "active_entries": total - expired,
            }

    @classmethod
    def _calculate_ttl(cls) -> float:
        """
        Calculate a jittered TTL.

        Random within base_ttl +/- jitter_range to desynchronize expiry
        across processes (thundering-herd prevention).
        """
        jitter_range = cls._get_jitter_range()
        jitter = random.uniform(-jitter_range, jitter_range)
        return cls._get_base_ttl() + jitter

    @classmethod
    def reset(cls) -> None:
        """Reset state (for tests)."""
        cls._delegate.invalidate_all()
        cls._fetch_callback = None
