"""
Pre-computed Cache Service - Multi-Tier Cache Access with Drift Detection.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import structlog

from baldur.core.singleflight import Singleflight

from .constants import (
    fast_json_dumps,
    fast_json_loads,
    record_cache_drift,
    update_cache_consistency,
    update_cache_hit_rate,
)
from .l1_cache import _l1_cache
from .l2_cache import _l2_cache

logger = structlog.get_logger()


# =============================================================================
# Multi-Tier Cache Access with Drift Detection
# =============================================================================

# L3 compute deduplication: concurrent same-key misses (cold start,
# simultaneous L1+L2 expiry) run compute_fn exactly once per process.
# Results are shared as the serialized JSON string so every caller
# deserializes into its OWN dict (no shared mutable response object).
# The map self-cleans, so no test-reset hook is needed.
_l3_singleflight: Singleflight[str] = Singleflight()

# Drift tracking stats per cache key
_drift_stats: dict[str, dict[str, int]] = {}
_drift_stats_lock = threading.Lock()


def _get_drift_stats(cache_key: str) -> dict[str, int]:
    """Get or create drift stats for a cache key."""
    with _drift_stats_lock:
        if cache_key not in _drift_stats:
            _drift_stats[cache_key] = {
                "l1_hits": 0,
                "l2_hits": 0,
                "l3_fallbacks": 0,
                "drift_count": 0,
                "total_accesses": 0,
            }
        return _drift_stats[cache_key]


def get_drift_stats_all() -> dict[str, dict[str, int]]:
    """Get drift stats for all cache keys."""
    with _drift_stats_lock:
        return {k: v.copy() for k, v in _drift_stats.items()}


def reset_drift_stats() -> None:
    """Reset drift tracking stats for test isolation."""
    global _drift_stats
    with _drift_stats_lock:
        _drift_stats.clear()


def get_cached_response(  # noqa: C901, PLR0915
    cache_key: str,
    compute_fn: Callable[[], dict[str, Any]],
    use_l1: bool = True,
    use_l2: bool = True,
) -> dict[str, Any]:
    """
    Get response from multi-tier cache with fallback to compute.

    v6.3.0: Added Drift Detection metrics

    Flow:
    1. Check L1 (in-process) → 0ms if hit
    2. Check L2 (Redis) → 1-5ms if hit
    3. Compute fresh → 50-200ms

    Args:
        cache_key: Cache key for this endpoint
        compute_fn: Function to compute fresh response
        use_l1: Whether to use L1 cache
        use_l2: Whether to use L2 cache

    Returns:
        Response data dict
    """
    start_time = time.perf_counter()
    stats = _get_drift_stats(cache_key)
    stats["total_accesses"] += 1

    # L1: In-process cache
    if use_l1:
        l1_value = _l1_cache.get(cache_key)
        if l1_value:
            stats["l1_hits"] += 1
            data = fast_json_loads(l1_value)
            data["_cache"] = {
                "hit": "L1",
                "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
            }
            _update_hit_rate_metrics(cache_key, stats)
            return data

    # L2: Redis cache
    if use_l2:
        l2_value = _l2_cache.get(cache_key)
        if l2_value:
            stats["l2_hits"] += 1
            # Populate L1 from L2
            if use_l1:
                _l1_cache.set(cache_key, l2_value)
            data = fast_json_loads(l2_value)
            data["_cache"] = {
                "hit": "L2",
                "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
            }
            _update_hit_rate_metrics(cache_key, stats)
            return data

    # L3: Direct compute (with CB protection)
    stats["l3_fallbacks"] += 1

    # Lazy import to avoid circular dependency (worker imports multi_tier)
    from .worker import get_precomputed_cache_worker

    worker = get_precomputed_cache_worker()
    cb = worker.cb_service
    cb_service_name = "precomputed_cache_compute"

    # CB OPEN → serve stale or static fallback
    if cb and not cb.should_allow(cb_service_name):
        stale = _l1_cache.get_stale(cache_key)
        if stale:
            logger.warning("precomputed_cache.stale_served", cache_key=cache_key)
            data = fast_json_loads(stale)
            data["_cache"] = {
                "hit": "STALE",
                "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
            }
            _update_hit_rate_metrics(cache_key, stats)
            return data
        _update_hit_rate_metrics(cache_key, stats)
        logger.warning(
            "precomputed_cache.static_fallback_served",
            cache_key=cache_key,
        )
        return {
            "status": "unavailable",
            "reason": "circuit_breaker_open",
            "_cache": {
                "hit": "CB_OPEN",
                "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
            },
        }

    # Singleflight the compute: only the winner runs the closure below;
    # concurrent same-key callers (waiters) share its serialized result
    # - or its exception. CB accounting and the failure log happen
    # inside the closure, exactly once per actual backend call.
    computed_by_me = False

    def _compute_and_store() -> str:
        nonlocal computed_by_me
        computed_by_me = True
        try:
            data = compute_fn()
            data["_cache"] = {"hit": "MISS", "computed": True}

            if cb:
                cb.record_success(cb_service_name)

            # Cache the result
            json_str = fast_json_dumps(data)
            if use_l1:
                _l1_cache.set(cache_key, json_str)
            if use_l2:
                _l2_cache.set(cache_key, json_str)

            return json_str

        except Exception as e:
            if cb:
                cb.record_failure(cb_service_name)

            logger.exception(
                "precomputed_cache.compute_failed",
                cache_key=cache_key,
                error=e,
            )
            raise

    try:
        json_str = _l3_singleflight.run(cache_key, _compute_and_store)
    except Exception as e:
        # Each caller builds its own error dict from the (possibly
        # shared) exception - the per-caller error response shape is
        # preserved.
        return {
            "status": "error",
            "error": str(e),
            "_cache": {
                "hit": "ERROR",
                "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
            },
        }

    # Every caller deserializes into its OWN dict and attaches its own
    # _cache metadata (no shared mutable response object).
    data = fast_json_loads(json_str)
    if computed_by_me:
        data["_cache"] = {"hit": "MISS", "computed": True}
    else:
        data["_cache"] = {"hit": "DEDUP"}
    data["_cache"]["latency_ms"] = round((time.perf_counter() - start_time) * 1000, 2)
    _update_hit_rate_metrics(cache_key, stats)
    return data


def _update_hit_rate_metrics(cache_key: str, stats: dict[str, int]) -> None:
    """Update Prometheus hit rate metrics."""
    total = stats["total_accesses"]
    if total > 0:
        l1_rate = stats["l1_hits"] / total
        l2_rate = stats["l2_hits"] / total
        l3_rate = stats["l3_fallbacks"] / total
        update_cache_hit_rate(cache_key, "l1", l1_rate)
        update_cache_hit_rate(cache_key, "l2", l2_rate)
        update_cache_hit_rate(cache_key, "l3", l3_rate)


def check_l1_l2_drift(cache_key: str) -> dict[str, Any] | None:
    """
    Compare L1 and L2 cache values to detect drift.

    Returns:
        Drift info dict if drift detected, None otherwise
    """
    l1_value = _l1_cache.get(cache_key)
    l2_value = _l2_cache.get(cache_key)

    if l1_value is None or l2_value is None:
        # Cannot check drift when either cache is empty
        return None

    try:
        l1_data = fast_json_loads(l1_value)
        l2_data = fast_json_loads(l2_value)

        # Compare excluding _cache metadata
        l1_compare = {k: v for k, v in l1_data.items() if not k.startswith("_")}
        l2_compare = {k: v for k, v in l2_data.items() if not k.startswith("_")}

        if l1_compare != l2_compare:
            stats = _get_drift_stats(cache_key)
            stats["drift_count"] += 1

            # Record Prometheus metrics
            record_cache_drift(cache_key, severity="warning")

            # Calculate consistency ratio (1.0 when no drift)
            total = stats["total_accesses"]
            drift_count = stats["drift_count"]
            consistency = 1.0 - (drift_count / total) if total > 0 else 1.0
            update_cache_consistency(cache_key, consistency)

            logger.warning(
                "precomputed_cache.drift_detected",
                cache_key=cache_key,
                drift_count=drift_count,
            )

            return {
                "cache_key": cache_key,
                "drift_count": drift_count,
                "consistency_ratio": consistency,
                "l1_data": l1_compare,
                "l2_data": l2_compare,
            }
        # Consistency maintained
        stats = _get_drift_stats(cache_key)
        total = stats["total_accesses"]
        drift_count = stats["drift_count"]
        consistency = 1.0 - (drift_count / total) if total > 0 else 1.0
        update_cache_consistency(cache_key, consistency)

    except Exception as e:
        logger.debug(
            "precomputed_cache.drift_check_failed",
            cache_key=cache_key,
            error=e,
        )

    return None
