"""
Pre-computed Cache Service for L3 Observability Endpoints.

V3 optimization: pre-computed cache service for keeping L3 overhead under 50ms.

Architecture:
- L1: In-process TTLCache (2s TTL) - 0ms overhead
- L2: Redis Pre-computed JSON (15s TTL) - 1-5ms overhead
- L3: Direct Compute (fallback) - 50-200ms overhead

Background Worker:
- Threading.Timer based (no Celery, 0 dependency)
- Pre-computes health/error-budget/pool-status on the configured
  refresh interval (default 10s)
- Started from AppConfig.ready()

Modules:
    - constants: Cache keys, config loaders, feature flags, JSON utils
    - l1_cache: L1 in-process TTLCache
    - l2_cache: L2 Redis pre-computed JSON cache
    - multi_tier: Multi-tier cache access with drift detection
    - worker: Background pre-computation worker
    - compute_functions: Compute functions for L3 endpoints

.. versionadded:: 6.4.0
    Converted from the flat ``precomputed_cache.py`` file to the
    ``precomputed_cache/`` package.
"""

# ---------------------------------------------------------------------------
# Dynamic attribute forwarding – expose ALL sub-module attributes at package
# level so that ``from baldur.services.precomputed_cache import _x``
# keeps working even for private helpers.
# ---------------------------------------------------------------------------
import sys as _sys

from baldur.services.precomputed_cache import (
    compute_functions as _compute_functions_mod,
)
from baldur.services.precomputed_cache import (
    constants as _constants_mod,
)
from baldur.services.precomputed_cache import (
    l1_cache as _l1_cache_mod,
)
from baldur.services.precomputed_cache import (
    l2_cache as _l2_cache_mod,
)
from baldur.services.precomputed_cache import (
    multi_tier as _multi_tier_mod,
)
from baldur.services.precomputed_cache import (
    worker as _worker_mod,
)
from baldur.services.precomputed_cache.compute_functions import (
    compute_error_budget_status,
    compute_health_status,
    compute_pool_status,
    get_cached_error_budget,
    get_cached_health,
    get_cached_pool_status,
    register_default_compute_functions,
)
from baldur.services.precomputed_cache.constants import (
    CACHE_KEY_ERROR_BUDGET,
    CACHE_KEY_HEALTH,
    CACHE_KEY_POOL_STATUS,
    HAS_CACHETOOLS,
    HAS_DRIFT_METRICS,
    HAS_ORJSON,
    fast_json_dumps,
    fast_json_loads,
)
from baldur.services.precomputed_cache.l1_cache import (
    L1Cache,
    get_l1_cache,
    reset_l1_cache,
)
from baldur.services.precomputed_cache.l2_cache import (
    L2RedisCache,
    get_l2_cache,
    reset_l2_cache,
)
from baldur.services.precomputed_cache.multi_tier import (
    check_l1_l2_drift,
    get_cached_response,
    get_drift_stats_all,
)
from baldur.services.precomputed_cache.worker import (
    PrecomputedCacheWorker,
    get_precomputed_cache_worker,
    reset_precomputed_cache_worker,
    start_precomputed_cache,
    stop_precomputed_cache,
)

_pkg = _sys.modules[__name__]
for _mod in (
    _constants_mod,
    _l1_cache_mod,
    _l2_cache_mod,
    _multi_tier_mod,
    _worker_mod,
    _compute_functions_mod,
):
    for _name in dir(_mod):
        if not _name.startswith("__") and not hasattr(_pkg, _name):
            setattr(_pkg, _name, getattr(_mod, _name))
del _name, _mod, _pkg

__all__ = [
    # Constants & Config
    "CACHE_KEY_HEALTH",
    "CACHE_KEY_ERROR_BUDGET",
    "CACHE_KEY_POOL_STATUS",
    "HAS_ORJSON",
    "HAS_CACHETOOLS",
    "HAS_DRIFT_METRICS",
    "fast_json_dumps",
    "fast_json_loads",
    # L1 Cache
    "L1Cache",
    "get_l1_cache",
    "reset_l1_cache",
    # L2 Cache
    "L2RedisCache",
    "get_l2_cache",
    "reset_l2_cache",
    # Multi-Tier
    "get_cached_response",
    "check_l1_l2_drift",
    "get_drift_stats_all",
    # Worker
    "PrecomputedCacheWorker",
    "get_precomputed_cache_worker",
    "reset_precomputed_cache_worker",
    "start_precomputed_cache",
    "stop_precomputed_cache",
    # Compute Functions
    "compute_health_status",
    "compute_error_budget_status",
    "compute_pool_status",
    "register_default_compute_functions",
    # Public API
    "get_cached_health",
    "get_cached_error_budget",
    "get_cached_pool_status",
]
