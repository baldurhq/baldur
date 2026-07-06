"""
Drift Detection Metrics for Baldur System.

이 모듈은 캐시, 계층형 저장소, 분산 복제 등에서 발생하는
불일치(Drift)를 추적하기 위한 Prometheus 메트릭을 정의합니다.

메트릭 카테고리:
- PoolCircuitBreaker: 커넥션 풀 서킷 브레이커 캐시 상태
- PrecomputedCache: L1/L2 캐시 일관성 및 Drift 감지
- EmergencyMode Cache: 비상 모드 캐시 상태
- RateLimiter: Redis 상태 및 Fallback 모드
- Config Cache: 설정 캐시 및 환경변수 변경 감지
- WAL Sync: Write-Ahead Log 동기화 상태
- ShadowLogger: L2 동기화 실패 추적
- TTLCache: TTL 기반 캐시 만료/퇴거
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import structlog

logger = structlog.get_logger()

# Try to import prometheus_client, but don't fail if not installed
try:
    from prometheus_client import REGISTRY, Counter, Gauge, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    if TYPE_CHECKING:
        from prometheus_client import REGISTRY, Counter, Gauge, Histogram
    else:
        Counter = None
        Gauge = None
        Histogram = None
        REGISTRY = None


# =============================================================================
# Safe Metric Registration Helpers (avoid duplicate registration)
# =============================================================================


def _get_or_create_counter(
    name: str, description: str, labels: list[str] | None = None
) -> Counter | None:
    """Get existing counter or create new one to avoid duplicate registration."""
    if not PROMETHEUS_AVAILABLE:
        return None
    if name in REGISTRY._names_to_collectors:
        return cast("Counter", REGISTRY._names_to_collectors[name])
    try:
        return Counter(name, description, labels or [])
    except ValueError:
        return cast("Counter | None", REGISTRY._names_to_collectors.get(name))


def _get_or_create_gauge(
    name: str, description: str, labels: list[str] | None = None
) -> Gauge | None:
    """Get existing gauge or create new one to avoid duplicate registration."""
    if not PROMETHEUS_AVAILABLE:
        return None
    if name in REGISTRY._names_to_collectors:
        return cast("Gauge", REGISTRY._names_to_collectors[name])
    try:
        return Gauge(name, description, labels or [])
    except ValueError:
        return cast("Gauge | None", REGISTRY._names_to_collectors.get(name))


def _get_or_create_histogram(
    name: str,
    description: str,
    labels: list[str] | None = None,
    buckets: tuple[float, ...] | None = None,
) -> Histogram | None:
    """Get existing histogram or create new one to avoid duplicate registration."""
    if not PROMETHEUS_AVAILABLE:
        return None
    if name in REGISTRY._names_to_collectors:
        return cast("Histogram", REGISTRY._names_to_collectors[name])
    try:
        if buckets:
            return Histogram(name, description, labels or [], buckets=buckets)
        return Histogram(name, description, labels or [])
    except ValueError:
        return cast("Histogram | None", REGISTRY._names_to_collectors.get(name))


# =============================================================================
# Metric Prefix
# =============================================================================

METRIC_PREFIX = "baldur"


# =============================================================================
# PoolCircuitBreaker Metrics - 커넥션 풀 서킷 브레이커 캐시 상태
# =============================================================================

# PoolCircuitBreaker Stale Cache 메트릭
pool_cb_cache_stale_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_pool_cb_cache_stale_total",
    "Total stale cache events in PoolCircuitBreaker",
    ["severity"],  # warning, critical
)

pool_cb_cache_age_ms = _get_or_create_histogram(
    f"{METRIC_PREFIX}_pool_cb_cache_age_ms",
    "Age of PoolCircuitBreaker cache when accessed (ms)",
    [],
    buckets=(10, 50, 100, 200, 500, 1000, 2000, 5000),
)

pool_cb_cache_hit_rate = _get_or_create_gauge(
    f"{METRIC_PREFIX}_pool_cb_cache_hit_rate",
    "PoolCircuitBreaker cache hit rate",
)

pool_cb_background_restarts_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_pool_cb_background_restarts_total",
    "Number of background thread restarts in PoolCircuitBreaker",
)


# =============================================================================
# PrecomputedCache Metrics - L1/L2 캐시 일관성 및 Drift 감지
# =============================================================================

# PrecomputedCache Drift 메트릭
cache_drift_detected_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_cache_drift_detected_total",
    "Total cache drift detections between L1 and L2",
    ["cache_key", "severity"],  # severity: warning, critical
)

cache_l1_l2_consistency = _get_or_create_gauge(
    f"{METRIC_PREFIX}_cache_l1_l2_consistency",
    "L1/L2 cache consistency ratio (1.0 = fully consistent)",
    ["cache_key"],
)

cache_hit_rate = _get_or_create_gauge(
    f"{METRIC_PREFIX}_cache_hit_rate",
    "Cache hit rate per layer",
    ["cache_key", "layer"],  # layer: l1, l2, l3
)

cache_refresh_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_cache_refresh_total",
    "Total cache refresh operations",
    ["cache_key", "status"],  # status: success, failed
)


# =============================================================================
# EmergencyMode Cache Metrics - 비상 모드 캐시 상태
# =============================================================================

# EmergencyMode Cache 메트릭
emergency_cache_stale_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_emergency_cache_stale_total",
    "Number of times emergency mode cache became stale",
)

emergency_cache_drift_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_emergency_cache_drift_total",
    "Number of times cached state differed from backend",
)

emergency_cache_age_seconds = _get_or_create_gauge(
    f"{METRIC_PREFIX}_emergency_cache_age_seconds",
    "Current age of emergency mode cache in seconds",
)

emergency_cache_load_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_emergency_cache_load_total",
    "Number of times state was loaded from backend",
    ["reason"],  # reason: expired, invalidated, startup
)


# =============================================================================
# RateLimiter Metrics - Redis 상태 및 Fallback 모드
# =============================================================================

# RateLimiter Redis Drift 메트릭
ratelimit_redis_unavailable_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_ratelimit_redis_unavailable_total",
    "Number of times Redis was unavailable for rate limiting",
)

ratelimit_state_drift_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_ratelimit_state_drift_total",
    "Number of rate limit state drifts detected",
    ["key"],
)

ratelimit_fallback_active = _get_or_create_gauge(
    f"{METRIC_PREFIX}_ratelimit_fallback_active",
    "Whether rate limiter is in fallback mode (1=yes, 0=no)",
)

ratelimit_reconciliation_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_ratelimit_reconciliation_total",
    "Number of rate limit state reconciliations after Redis recovery",
    ["result"],  # result: success, failed
)


# =============================================================================
# Config Cache Metrics - 설정 캐시 및 환경변수 변경 감지
# =============================================================================

# Config lru_cache 메트릭
config_env_changed_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_config_env_changed_total",
    "Number of environment variable changes detected",
    ["config_type"],
)

config_cache_invalidated_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_config_cache_invalidated_total",
    "Number of config cache invalidations",
    ["config_type"],
)

config_cache_hit_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_config_cache_hit_total",
    "Number of config cache hits",
    ["config_type"],
)

config_cache_miss_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_config_cache_miss_total",
    "Number of config cache misses (recomputed)",
    ["config_type"],
)


# =============================================================================
# WAL Sync Metrics - Write-Ahead Log 동기화 상태
# =============================================================================

# WAL 동기화 Drift 메트릭
wal_entries_written_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_wal_entries_written_total",
    "Total WAL entries written",
)

wal_entries_recovered_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_wal_entries_recovered_total",
    "Total WAL entries recovered",
)

wal_orphans_absorbed_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_wal_orphans_absorbed_total",
    "Total orphan (non-own-PID) WAL entries absorbed at worker startup",
)

wal_corruption_detected_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_wal_corruption_detected_total",
    "Number of WAL corruption events detected",
)

wal_rotation_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_wal_rotation_total",
    "Number of WAL file rotations",
)

wal_sync_lag_entries = _get_or_create_gauge(
    f"{METRIC_PREFIX}_wal_sync_lag_entries",
    "Number of WAL entries pending sync to central storage",
)

wal_last_sequence = _get_or_create_gauge(
    f"{METRIC_PREFIX}_wal_last_sequence",
    "Last WAL sequence number written",
)

wal_corrupted_lines_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_wal_corrupted_lines_total",
    "Total JSONL WAL corrupted lines skipped during read",
)

wal_total_files = _get_or_create_gauge(
    f"{METRIC_PREFIX}_wal_total_files",
    "Current number of WAL files on disk (file rotation health)",
)

wal_current_size_bytes = _get_or_create_gauge(
    f"{METRIC_PREFIX}_wal_current_size_bytes",
    "Current WAL file size in bytes (per-file disk pressure)",
)

wal_disk_full_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_wal_disk_full_total",
    "Number of WAL disk-full events (post-purge fail-open transitions)",
)

wal_sync_cursor_stalled = _get_or_create_gauge(
    f"{METRIC_PREFIX}_wal_sync_cursor_stalled",
    "Whether the WAL sync cursor is stalled on a permanently-failing entry "
    "(1=stalled/audit-loss risk, 0=healthy)",
)


# =============================================================================
# ShadowLogger Metrics - L2 동기화 실패 추적
# =============================================================================

# ShadowLogger L2 동기화 Drift 메트릭
shadow_log_sync_failures_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_shadow_log_sync_failures_total",
    "Total L2 sync failures recorded in shadow log",
    ["adapter_type", "operation"],  # operation: sync, update, delete
)

shadow_log_unsynced_count = _get_or_create_gauge(
    f"{METRIC_PREFIX}_shadow_log_unsynced_count",
    "Number of shadow log records not yet synced to L2",
)

shadow_log_recovered_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_shadow_log_recovered_total",
    "Total shadow log records recovered after L2 recovery",
    ["service_name"],
)

shadow_log_affected_services = _get_or_create_gauge(
    f"{METRIC_PREFIX}_shadow_log_affected_services",
    "Number of services affected by L2 sync failures",
)

shadow_log_oldest_unsynced_age_seconds = _get_or_create_gauge(
    f"{METRIC_PREFIX}_shadow_log_oldest_unsynced_age_seconds",
    "Age of oldest unsynced shadow log record in seconds",
)


# =============================================================================
# Cache Adapter Operation Errors - swallowed exceptions in cache adapters
# =============================================================================

# Cache adapters (Redis, Memcached, Memory) follow a "swallow + safe-default"
# convention: internal exceptions are caught, logged via logger.exception, and
# the call returns a safe default (None, [], 0). This counter surfaces those
# swallowed errors as an SLI so callers can alert on cache backend health
# without changing the swallow contract.
cache_operation_errors_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_cache_operation_errors_total",
    "Cache adapter operation errors swallowed by the adapter",
    [
        "backend",
        "operation",
    ],  # backend: redis/memory/memcached, operation: get/set/push_limit/list_range/...
)


# =============================================================================
# TTLCache Metrics - TTL 기반 캐시 만료/퇴거
# =============================================================================

# TTLCache Strategy Drift 메트릭
cache_ttl_expired_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_cache_ttl_expired_total",
    "Total cache entries expired by TTL",
    ["cache_name"],
)

cache_ttl_evicted_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_cache_ttl_evicted_total",
    "Total cache entries evicted (capacity limit)",
    ["cache_name"],
)

cache_entries_count = _get_or_create_gauge(
    f"{METRIC_PREFIX}_cache_entries_count",
    "Current number of entries in cache",
    ["cache_name"],
)

cache_get_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_cache_get_total",
    "Total cache get operations",
    ["cache_name", "result"],  # result: hit, miss, expired
)

cache_set_total = _get_or_create_counter(
    f"{METRIC_PREFIX}_cache_set_total",
    "Total cache set operations",
    ["cache_name"],
)


# =============================================================================
# Helper Functions
# =============================================================================


def record_pool_cb_stale(severity: str) -> None:
    """Record a PoolCircuitBreaker stale cache event."""
    if pool_cb_cache_stale_total is not None:
        pool_cb_cache_stale_total.labels(severity=severity).inc()


def record_pool_cb_cache_age(age_ms: float) -> None:
    """Record PoolCircuitBreaker cache age."""
    if pool_cb_cache_age_ms is not None:
        pool_cb_cache_age_ms.observe(age_ms)


def update_pool_cb_hit_rate(hit_rate: float) -> None:
    """Update PoolCircuitBreaker cache hit rate."""
    if pool_cb_cache_hit_rate is not None:
        pool_cb_cache_hit_rate.set(hit_rate)


def record_pool_cb_background_restart() -> None:
    """Record a background thread restart."""
    if pool_cb_background_restarts_total is not None:
        pool_cb_background_restarts_total.inc()


def record_cache_drift(cache_key: str, severity: str = "warning") -> None:
    """Record a cache drift detection event."""
    if cache_drift_detected_total is not None:
        cache_drift_detected_total.labels(
            cache_key=cache_key,
            severity=severity,
        ).inc()


def update_cache_consistency(cache_key: str, ratio: float) -> None:
    """Update cache consistency ratio (0.0 to 1.0)."""
    if cache_l1_l2_consistency is not None:
        cache_l1_l2_consistency.labels(cache_key=cache_key).set(ratio)


def update_cache_hit_rate(cache_key: str, layer: str, rate: float) -> None:
    """Update cache hit rate for a specific layer."""
    if cache_hit_rate is not None:
        cache_hit_rate.labels(cache_key=cache_key, layer=layer).set(rate)


def record_cache_refresh(cache_key: str, success: bool) -> None:
    """Record a cache refresh operation."""
    if cache_refresh_total is not None:
        status = "success" if success else "failed"
        cache_refresh_total.labels(cache_key=cache_key, status=status).inc()


def record_emergency_cache_stale() -> None:
    """Record an emergency cache stale event."""
    if emergency_cache_stale_total is not None:
        emergency_cache_stale_total.inc()


def record_emergency_cache_drift() -> None:
    """Record an emergency cache drift event."""
    if emergency_cache_drift_total is not None:
        emergency_cache_drift_total.inc()


def update_emergency_cache_age(age_seconds: float) -> None:
    """Update emergency cache age gauge."""
    if emergency_cache_age_seconds is not None:
        emergency_cache_age_seconds.set(age_seconds)


def record_emergency_cache_load(reason: str) -> None:
    """Record a state load from backend."""
    if emergency_cache_load_total is not None:
        emergency_cache_load_total.labels(reason=reason).inc()


def record_ratelimit_redis_unavailable() -> None:
    """Record Redis unavailability for rate limiting."""
    if ratelimit_redis_unavailable_total is not None:
        ratelimit_redis_unavailable_total.inc()


def record_ratelimit_drift(key: str) -> None:
    """Record a rate limit state drift."""
    if ratelimit_state_drift_total is not None:
        ratelimit_state_drift_total.labels(key=key).inc()


def set_ratelimit_fallback_mode(active: bool) -> None:
    """Set rate limiter fallback mode status."""
    if ratelimit_fallback_active is not None:
        ratelimit_fallback_active.set(1 if active else 0)


def record_ratelimit_reconciliation(success: bool) -> None:
    """Record a rate limit reconciliation."""
    if ratelimit_reconciliation_total is not None:
        result = "success" if success else "failed"
        ratelimit_reconciliation_total.labels(result=result).inc()


def record_config_env_changed(config_type: str) -> None:
    """Record an environment variable change detection."""
    if config_env_changed_total is not None:
        config_env_changed_total.labels(config_type=config_type).inc()


def record_config_cache_invalidated(config_type: str) -> None:
    """Record a config cache invalidation."""
    if config_cache_invalidated_total is not None:
        config_cache_invalidated_total.labels(config_type=config_type).inc()


def record_config_cache_hit(config_type: str) -> None:
    """Record a config cache hit."""
    if config_cache_hit_total is not None:
        config_cache_hit_total.labels(config_type=config_type).inc()


def record_config_cache_miss(config_type: str) -> None:
    """Record a config cache miss."""
    if config_cache_miss_total is not None:
        config_cache_miss_total.labels(config_type=config_type).inc()


# =============================================================================
# WAL Sync Helper Functions
# =============================================================================


def record_wal_entry_written() -> None:
    """Record a WAL entry written."""
    if wal_entries_written_total is not None:
        wal_entries_written_total.inc()


def record_wal_entries_recovered(count: int) -> None:
    """Record WAL entries recovered."""
    if wal_entries_recovered_total is not None:
        wal_entries_recovered_total.inc(count)


def record_wal_orphans_absorbed(count: int) -> None:
    """Record orphan WAL entries absorbed at worker startup.

    Surfaces multi-worker crash/orphan-absorption frequency as an SLI so SRE
    has infra-level visibility beyond the INFO log line.
    """
    if wal_orphans_absorbed_total is not None:
        wal_orphans_absorbed_total.inc(count)


def record_wal_corruption() -> None:
    """Record a WAL corruption event."""
    if wal_corruption_detected_total is not None:
        wal_corruption_detected_total.inc()


def record_wal_rotation() -> None:
    """Record a WAL file rotation."""
    if wal_rotation_total is not None:
        wal_rotation_total.inc()


def update_wal_sync_lag(lag: int) -> None:
    """Update WAL sync lag (pending entries)."""
    if wal_sync_lag_entries is not None:
        wal_sync_lag_entries.set(lag)


def update_wal_cursor_stalled(stalled: bool) -> None:
    """Set the WAL sync cursor-stall gauge (1=stalled, 0=healthy).

    Fed by AuditSyncWorker when a permanently-failing head entry pins the
    contiguous cursor. Surfaces a compliance audit-loss-risk signal as a
    threshold-alertable gauge rather than a log-scrape-only event.
    """
    if wal_sync_cursor_stalled is not None:
        wal_sync_cursor_stalled.set(1 if stalled else 0)


def update_wal_last_sequence(seq: int) -> None:
    """Update last WAL sequence number."""
    if wal_last_sequence is not None:
        wal_last_sequence.set(seq)


def record_wal_corrupted_line() -> None:
    """Record a JSONL WAL corrupted line skipped during read."""
    if wal_corrupted_lines_total is not None:
        wal_corrupted_lines_total.inc()


def update_wal_total_files(count: int) -> None:
    """Update current WAL file count (set by periodic refresh task)."""
    if wal_total_files is not None:
        wal_total_files.set(count)


def update_wal_current_size_bytes(size: int) -> None:
    """Update current WAL file size in bytes (set by periodic refresh task)."""
    if wal_current_size_bytes is not None:
        wal_current_size_bytes.set(size)


def record_wal_disk_full() -> None:
    """Record a WAL disk-full fail-open transition."""
    if wal_disk_full_total is not None:
        wal_disk_full_total.inc()


# =============================================================================
# ShadowLogger Helper Functions
# =============================================================================


def record_shadow_log_sync_failure(adapter_type: str, operation: str) -> None:
    """Record an L2 sync failure in shadow log."""
    if shadow_log_sync_failures_total is not None:
        shadow_log_sync_failures_total.labels(
            adapter_type=adapter_type,
            operation=operation,
        ).inc()


def update_shadow_log_unsynced_count(count: int) -> None:
    """Update count of unsynced shadow log records."""
    if shadow_log_unsynced_count is not None:
        shadow_log_unsynced_count.set(count)


def record_shadow_log_recovered(service_name: str, count: int) -> None:
    """Record shadow log records recovered."""
    if shadow_log_recovered_total is not None:
        shadow_log_recovered_total.labels(service_name=service_name).inc(count)


def update_shadow_log_affected_services(count: int) -> None:
    """Update count of affected services."""
    if shadow_log_affected_services is not None:
        shadow_log_affected_services.set(count)


def update_shadow_log_oldest_unsynced_age(age_seconds: float) -> None:
    """Update age of oldest unsynced record."""
    if shadow_log_oldest_unsynced_age_seconds is not None:
        shadow_log_oldest_unsynced_age_seconds.set(age_seconds)


# =============================================================================
# TTLCache Helper Functions
# =============================================================================


def record_cache_ttl_expired(cache_name: str) -> None:
    """Record a cache entry expired by TTL."""
    if cache_ttl_expired_total is not None:
        cache_ttl_expired_total.labels(cache_name=cache_name).inc()


def record_cache_ttl_evicted(cache_name: str) -> None:
    """Record a cache entry evicted due to capacity."""
    if cache_ttl_evicted_total is not None:
        cache_ttl_evicted_total.labels(cache_name=cache_name).inc()


def update_cache_entries_count(cache_name: str, count: int) -> None:
    """Update current cache entry count."""
    if cache_entries_count is not None:
        cache_entries_count.labels(cache_name=cache_name).set(count)


def record_cache_get(cache_name: str, result: str) -> None:
    """Record a cache get operation. result: hit, miss, expired"""
    if cache_get_total is not None:
        cache_get_total.labels(cache_name=cache_name, result=result).inc()


def record_cache_set(cache_name: str) -> None:
    """Record a cache set operation."""
    if cache_set_total is not None:
        cache_set_total.labels(cache_name=cache_name).inc()


def record_cache_operation_error(backend: str, operation: str) -> None:
    """Record a swallowed cache adapter operation error.

    Called from inside cache adapter except blocks (where the exception is
    caught and the call returns a safe default). Surfaces the swallowed
    failure as an SLI without changing the adapter contract.

    Args:
        backend: Adapter type (e.g., "redis", "memory", "memcached")
        operation: Operation name (e.g., "get", "set", "push_limit", "list_range")
    """
    if cache_operation_errors_total is not None:
        cache_operation_errors_total.labels(backend=backend, operation=operation).inc()


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # PoolCircuitBreaker
    "pool_cb_cache_stale_total",
    "pool_cb_cache_age_ms",
    "pool_cb_cache_hit_rate",
    "pool_cb_background_restarts_total",
    "record_pool_cb_stale",
    "record_pool_cb_cache_age",
    "update_pool_cb_hit_rate",
    "record_pool_cb_background_restart",
    # PrecomputedCache
    "cache_drift_detected_total",
    "cache_l1_l2_consistency",
    "cache_hit_rate",
    "cache_refresh_total",
    "record_cache_drift",
    "update_cache_consistency",
    "update_cache_hit_rate",
    "record_cache_refresh",
    # EmergencyMode
    "emergency_cache_stale_total",
    "emergency_cache_drift_total",
    "emergency_cache_age_seconds",
    "emergency_cache_load_total",
    "record_emergency_cache_stale",
    "record_emergency_cache_drift",
    "update_emergency_cache_age",
    "record_emergency_cache_load",
    # RateLimiter
    "ratelimit_redis_unavailable_total",
    "ratelimit_state_drift_total",
    "ratelimit_fallback_active",
    "ratelimit_reconciliation_total",
    "record_ratelimit_redis_unavailable",
    "record_ratelimit_drift",
    "set_ratelimit_fallback_mode",
    "record_ratelimit_reconciliation",
    # Config Cache
    "config_env_changed_total",
    "config_cache_invalidated_total",
    "config_cache_hit_total",
    "config_cache_miss_total",
    "record_config_env_changed",
    "record_config_cache_invalidated",
    "record_config_cache_hit",
    "record_config_cache_miss",
    # WAL Sync
    "wal_entries_written_total",
    "wal_entries_recovered_total",
    "wal_orphans_absorbed_total",
    "wal_corruption_detected_total",
    "wal_rotation_total",
    "wal_sync_lag_entries",
    "wal_last_sequence",
    "record_wal_entry_written",
    "record_wal_entries_recovered",
    "record_wal_orphans_absorbed",
    "record_wal_corruption",
    "record_wal_rotation",
    "update_wal_sync_lag",
    "update_wal_last_sequence",
    "wal_corrupted_lines_total",
    "record_wal_corrupted_line",
    "wal_total_files",
    "wal_current_size_bytes",
    "update_wal_total_files",
    "update_wal_current_size_bytes",
    "wal_disk_full_total",
    "record_wal_disk_full",
    "wal_sync_cursor_stalled",
    "update_wal_cursor_stalled",
    # ShadowLogger
    "shadow_log_sync_failures_total",
    "shadow_log_unsynced_count",
    "shadow_log_recovered_total",
    "shadow_log_affected_services",
    "shadow_log_oldest_unsynced_age_seconds",
    "record_shadow_log_sync_failure",
    "update_shadow_log_unsynced_count",
    "record_shadow_log_recovered",
    "update_shadow_log_affected_services",
    "update_shadow_log_oldest_unsynced_age",
    # TTLCache
    "cache_ttl_expired_total",
    "cache_ttl_evicted_total",
    "cache_entries_count",
    "cache_get_total",
    "cache_set_total",
    "record_cache_ttl_expired",
    "record_cache_ttl_evicted",
    "update_cache_entries_count",
    "record_cache_get",
    "record_cache_set",
    # Cache Adapter Operation Errors
    "cache_operation_errors_total",
    "record_cache_operation_error",
    # Utils
    "PROMETHEUS_AVAILABLE",
]
