"""
Metric Registration Helpers, Domain Registry, and Label Utilities.

Provides safe metric registration to avoid duplicate registration errors,
dynamic domain management for metric labeling,
label sanitization for Prometheus safety,
and batch metric recording for high-throughput paths.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from collections.abc import Callable
from typing import Any, cast

import structlog

logger = structlog.get_logger()

try:
    from prometheus_client import REGISTRY, Counter, Gauge, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    # Sentinel; helpers raise before touching it. Typed Any so the
    # try-branch import (CollectorRegistry) and the except-branch None
    # share a compatible annotation across conditional signatures.
    REGISTRY: Any = None  # type: ignore[no-redef]

if not PROMETHEUS_AVAILABLE:
    logger.warning("metrics.prometheus_unavailable")


_PROMETHEUS_INSTALL_HINT = (
    "prometheus_client is required for metric registration but is not installed. "
    'Install with: pip install "baldur-framework[prometheus]" '
    "(quotes required in zsh/fish to prevent bracket glob expansion)."
)


# =============================================================================
# Prometheus Label Sanitization
# =============================================================================

_LABEL_UNSAFE_PATTERN = re.compile(r"[^a-zA-Z0-9_]")
UNKNOWN_LABEL_VALUE = "unknown"
DEFAULT_LABEL_MAX_LENGTH = 128


def sanitize_label_value(value: str, max_length: int = DEFAULT_LABEL_MAX_LENGTH) -> str:
    """
    Prometheus 메트릭 라벨 값을 안전한 형식으로 정규화.

    영숫자/언더스코어 이외 문자는 '_'로 치환하고,
    최대 길이 128자로 절단하며, 빈 문자열은 'unknown'을 반환합니다.

    Examples:
        >>> sanitize_label_value("my-service.v2")
        'my_service_v2'
        >>> sanitize_label_value("")
        'unknown'
    """
    if not value or not value.strip():
        return UNKNOWN_LABEL_VALUE
    sanitized = _LABEL_UNSAFE_PATTERN.sub("_", value.strip())
    return sanitized[:max_length]


# =============================================================================
# Metrics Batch Recorder (async batch for hot paths)
# =============================================================================


class MetricsBatchRecorder:
    """
    핫 패스에서 메트릭 기록을 비동기 배치로 처리.

    호출 스레드는 SimpleQueue.put()만 수행 (Lock-free, ~50ns).
    백그라운드 데몬 스레드가 100ms 간격 또는 배치 크기 256 도달 시 flush.
    flush 실패 시 해당 배치를 drop하고 경고 로깅 (Fail-Open).
    """

    __slots__ = (
        "_queue",
        "_batch_size",
        "_flush_interval",
        "_worker",
        "_running",
    )

    def __init__(
        self,
        batch_size: int = 256,
        flush_interval_ms: int = 100,
    ) -> None:
        self._queue: queue.SimpleQueue[tuple[Callable, tuple, dict]] = (
            queue.SimpleQueue()
        )
        self._batch_size = batch_size
        self._flush_interval = flush_interval_ms / 1000.0
        self._running = True
        self._worker = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="metrics-batch-recorder",
        )
        self._worker.start()

    def enqueue(
        self,
        metric_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """
        메트릭 기록 요청을 큐에 적재 — Lock-free O(1).

        핫 패스에서 호출. SimpleQueue.put()은 Lock-free이므로
        prometheus_client 내부 Lock 경합을 회피합니다.
        """
        if self._running:
            self._queue.put((metric_fn, args, kwargs))

    def _flush_loop(self) -> None:
        """백그라운드 스레드: 배치 수집 후 일괄 기록."""
        while self._running:
            batch: list[tuple[Callable, tuple, dict]] = []
            deadline = time.monotonic() + self._flush_interval

            while len(batch) < self._batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=max(remaining, 0.001))
                    batch.append(item)
                except Exception:
                    break

            for metric_fn, args, kwargs in batch:
                try:
                    metric_fn(*args, **kwargs)
                except Exception as e:
                    logger.debug(
                        "metrics_batch_recorder.record_metric_failed",
                        error=e,
                    )

    def shutdown(self) -> None:
        """그레이스풀 셧다운 — 잔여 배치 flush."""
        self._running = False
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)


# =============================================================================
# Safe Metric Registration Helpers
# =============================================================================


def get_or_create_counter(name: str, description: str, labels: list[str]) -> Counter:
    """Get existing counter or create new one to avoid duplicate registration."""
    if not PROMETHEUS_AVAILABLE:
        raise ImportError(_PROMETHEUS_INSTALL_HINT)
    if name in REGISTRY._names_to_collectors:
        # _names_to_collectors values are Collector (the base class) at the
        # stub level; here the caller guarantees the name maps to a Counter.
        return cast(Counter, REGISTRY._names_to_collectors[name])
    try:
        return Counter(name, description, labels)
    except ValueError:
        return cast(Counter, REGISTRY._names_to_collectors[name])


def get_or_create_gauge(name: str, description: str, labels: list[str]) -> Gauge:
    """Get existing gauge or create new one to avoid duplicate registration."""
    if not PROMETHEUS_AVAILABLE:
        raise ImportError(_PROMETHEUS_INSTALL_HINT)
    if name in REGISTRY._names_to_collectors:
        return cast(Gauge, REGISTRY._names_to_collectors[name])
    try:
        return Gauge(name, description, labels)
    except ValueError:
        return cast(Gauge, REGISTRY._names_to_collectors[name])


def get_or_create_histogram(
    name: str,
    description: str,
    labels: list[str],
    buckets: tuple[float, ...] | None = None,
) -> Histogram:
    """Get existing histogram or create new one to avoid duplicate registration."""
    if not PROMETHEUS_AVAILABLE:
        raise ImportError(_PROMETHEUS_INSTALL_HINT)
    if name in REGISTRY._names_to_collectors:
        return cast(Histogram, REGISTRY._names_to_collectors[name])
    try:
        if buckets:
            return Histogram(name, description, labels, buckets=buckets)
        return Histogram(name, description, labels)
    except ValueError:
        return cast(Histogram, REGISTRY._names_to_collectors[name])


# =============================================================================
# Domain Registry (Dynamic Domain Registration)
# =============================================================================

_MAX_REGISTERED_DOMAINS = 50

# Fallback domain for unregistered domains — declared before _registered_domains
# so it can be included in the initial set. Single source of truth lives in
# ``utils/domain_validation`` (545 D1) so the metric label registry shares the
# same fallback string as DLQ/decorator rejection paths.
from baldur.utils.domain_validation import FALLBACK_DOMAIN as _FALLBACK_DOMAIN

# Default domains (domain-neutral fallbacks)
DEFAULT_DOMAINS: list[str] = [
    "external_service",
    "internal_process",
    "async_task",
    "notification",
    "data_sync",
]

# NOTE: Per-process registry. In multiprocess deployments (Gunicorn prefork,
# Celery workers), each process maintains its own copy. Since all processes
# execute the same registration code, the domain set is effectively identical.
# TSDB cardinality is bounded by max_registered_domains, not multiplied by
# worker count.
_registered_domains: set[str] = {
    "external_service",
    "internal_process",
    "async_task",
    "notification",
    "data_sync",
    _FALLBACK_DOMAIN,  # resolve idempotency — prevents spurious DEBUG logs
}


def _get_max_domains_from_settings() -> int:
    """Read max_registered_domains from MetricsSettings, fallback to module constant."""
    try:
        from baldur.settings.layered_provider import get_layered_settings
        from baldur.settings.metrics import MetricsSettings

        # Layered read so a console edit of the metrics domain takes effect
        # (686 D1/D5); env base when no RuntimeConfigManager is registered.
        return get_layered_settings(MetricsSettings, "metrics").max_registered_domains
    except Exception as exc:
        logger.warning(
            "metrics.settings_load_failed",
            fallback=_MAX_REGISTERED_DOMAINS,
            error=str(exc),
        )
        return _MAX_REGISTERED_DOMAINS


def register_domain(domain: str, *, max_domains: int | None = None) -> bool:
    """
    Register a domain for metrics collection.

    Args:
        domain: Domain name
        max_domains: Maximum number of registered domains.
            If None, reads from MetricsSettings.max_registered_domains.
            Falls back to _MAX_REGISTERED_DOMAINS (50) if settings unavailable.

    Returns:
        True if registration succeeded, False if limit exceeded
    """
    if max_domains is None:
        max_domains = _get_max_domains_from_settings()

    sanitized = sanitize_label_value(domain)
    if sanitized in _registered_domains:
        return True

    if len(_registered_domains) >= max_domains:
        logger.warning(
            "metrics.domain_registration_limit_reached",
            domain=domain,
            max_domains=max_domains,
            current_count=len(_registered_domains),
        )
        return False

    _registered_domains.add(sanitized)
    logger.debug(
        "metrics.domain_registered",
        domain=sanitized,
    )
    return True


def resolve_domain_label(domain: str) -> str:
    """
    Safely resolve a domain label for metric recording (enforcement).

    Returns the domain as-is if registered, otherwise forces OTHER_DOMAIN.
    Ensures that register_domain() limits are enforced, not advisory.

    Args:
        domain: Domain name

    Returns:
        Sanitized domain if registered, "OTHER_DOMAIN" otherwise
    """
    sanitized = sanitize_label_value(domain)
    if sanitized in _registered_domains:
        return sanitized
    logger.debug(
        "metrics.domain_label_unregistered",
        domain=domain,
        resolved_to=_FALLBACK_DOMAIN,
    )
    return _FALLBACK_DOMAIN


_DEFAULT_DOMAINS: frozenset[str] = frozenset(
    {
        "external_service",
        "internal_process",
        "async_task",
        "notification",
        "data_sync",
        _FALLBACK_DOMAIN,
    }
)


def reset_registered_domains() -> None:
    """Reset registered domains to defaults for test isolation.

    Uses clear() + update() instead of reassignment to preserve the set
    object identity — test fixtures may hold direct references to it.
    """
    _registered_domains.clear()
    _registered_domains.update(_DEFAULT_DOMAINS)


def get_registered_domains() -> list[str]:
    """Get all registered domains, including defaults."""
    all_domains = _registered_domains | set(DEFAULT_DOMAINS)
    return sorted(all_domains)
