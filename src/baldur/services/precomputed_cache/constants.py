"""
Pre-computed Cache Service - Constants and Configuration.

Cache keys, config loader functions, feature flags, and fast JSON serialization.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.utils.serialization import (
    FAST_JSON_AVAILABLE,
    fast_dumps_str,
    fast_loads,
)

HAS_ORJSON = FAST_JSON_AVAILABLE

try:
    from cachetools import TTLCache

    HAS_CACHETOOLS = True
except ImportError:
    TTLCache = None  # type: ignore[assignment,misc]
    HAS_CACHETOOLS = False

# Drift Detection Metrics
try:
    from baldur.metrics.drift_metrics import (
        record_cache_drift,
        record_cache_refresh,
        update_cache_consistency,
        update_cache_hit_rate,
    )

    HAS_DRIFT_METRICS = True
except ImportError:
    HAS_DRIFT_METRICS = False

    def record_cache_drift(cache_key: str, severity: str = "warning") -> None:
        return None

    def update_cache_consistency(cache_key: str, ratio: float) -> None:
        return None

    def update_cache_hit_rate(cache_key: str, layer: str, rate: float) -> None:
        return None

    def record_cache_refresh(cache_key: str, success: bool) -> None:
        return None


logger = structlog.get_logger()


# =============================================================================
# Constants (defaults, actual values loaded from Settings)
# =============================================================================


def _get_l1_ttl_seconds() -> float:
    """L1 TTL을 Settings에서 로드."""
    from baldur.settings.precomputed_cache import get_precomputed_cache_settings

    return get_precomputed_cache_settings().l1_ttl_seconds


def _get_l2_ttl_seconds() -> float:
    """L2 TTL을 Settings에서 로드."""
    from baldur.settings.precomputed_cache import get_precomputed_cache_settings

    return get_precomputed_cache_settings().l2_ttl_seconds


def _get_refresh_interval() -> float:
    """Refresh interval을 Settings에서 로드."""
    from baldur.settings.precomputed_cache import get_precomputed_cache_settings

    return get_precomputed_cache_settings().refresh_interval_seconds


def _get_l1_maxsize() -> int:
    """L1 maxsize를 Settings에서 로드."""
    from baldur.settings.precomputed_cache import get_precomputed_cache_settings

    return get_precomputed_cache_settings().l1_maxsize


# Cache Keys
CACHE_KEY_HEALTH = "baldur:cache:health"
CACHE_KEY_ERROR_BUDGET = "baldur:cache:error_budget"
CACHE_KEY_POOL_STATUS = "baldur:cache:pool_status"


# =============================================================================
# Fast JSON Serialization
# =============================================================================


def fast_json_dumps(data: Any) -> str:
    """Fast JSON serialization — delegates to ``baldur.utils.serialization``."""
    return fast_dumps_str(data)


def fast_json_loads(data: str) -> Any:
    """Fast JSON deserialization — delegates to ``baldur.utils.serialization``."""
    return fast_loads(data)
