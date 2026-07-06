"""
Core SafeGauge Implementation.

Thread-safe gauge wrapper that prevents negative values.

Design Philosophy:
- Counter Pair (Google SRE style) is technically superior but requires
  PromQL calculations on the dashboard side.
- SafeGauge provides "plug-and-play" experience for buyers while
  internally preventing the -1 dashboard embarrassment.

Enhanced Features (Metric Reliability):
- Sync Status Tracking: last_sync_time and is_synced for data freshness
- Staleness Detection: Auto-mark as stale after threshold
- Stabilization Period: Gradual recovery from strict mode

Memory Management (LRU Cache):
- 레이블 조합이 무한 증가하는 것을 방지하기 위한 LRU 캐시
- max_label_combinations로 최대 캐시 크기 설정
- Eviction 시 경고 로그 및 메트릭 기록
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import structlog

from .noop import NoOpGaugeChild
from .sync import SyncInfo

if TYPE_CHECKING:
    from prometheus_client import Gauge

logger = structlog.get_logger()


def _get_max_label_combinations() -> int:
    """SafeGaugeSettings에서 최대 레이블 조합 수를 가져온다."""
    try:
        from baldur.settings.safe_gauge import get_safe_gauge_settings

        return get_safe_gauge_settings().max_label_combinations
    except Exception:
        return 1000  # fallback


class SafeGaugeChild:
    """
    Safe wrapper for labeled Gauge child.

    Prevents the gauge from going negative by clamping at 0.
    This is critical for preventing "-1 pending items" on dashboards
    after server restarts when the in-memory counter starts at 0.

    Enhanced with sync status tracking:
    - Tracks last_sync_time for data freshness indication
    - Auto-detects staleness based on threshold
    - Supports stabilization period for gradual recovery

    Thread Safety:
        Uses a lock to ensure atomic read-check-update operations.
        This prevents race conditions in high-concurrency environments.

    Note:
        Prometheus client doesn't expose _value directly in a clean way,
        so we maintain our own shadow counter for clamping logic.
        The Lazy Sync (Reconciler) will correct any drift periodically.
    """

    def __init__(
        self,
        gauge_child: Any,
        label_values: dict[str, str],
        staleness_threshold: float = 300.0,
        stabilization_duration: float = 60.0,
    ):
        """
        Initialize SafeGaugeChild.

        Args:
            gauge_child: The original Prometheus Gauge child (labeled)
            label_values: Label key-value pairs for logging
            staleness_threshold: Seconds before data is considered stale (default: 5분)
            stabilization_duration: Seconds for gradual recovery (default: 60초)
        """
        self._gauge_child = gauge_child
        self._label_values = label_values
        self._lock = threading.Lock()
        # Shadow counter for clamping logic
        # Starts at 0, may drift from actual Prometheus value
        # Reconciler will sync periodically
        self._shadow_value: float = 0.0
        self._initialized = False

        # Sync status tracking
        self._sync_info = SyncInfo(
            staleness_threshold=staleness_threshold,
            stabilization_duration=stabilization_duration,
        )

    @property
    def sync_info(self) -> SyncInfo:
        """동기화 정보 조회."""
        return self._sync_info

    @property
    def is_synced(self) -> bool:
        """데이터 신뢰 가능 여부."""
        self._sync_info.check_staleness()
        return self._sync_info.is_synced

    @property
    def is_recovering(self) -> bool:
        """복구 중 여부."""
        return self._sync_info.is_recovering

    @property
    def last_sync_time(self) -> float | None:
        """마지막 동기화 시간."""
        return self._sync_info.last_sync_time

    @property
    def sync_age_seconds(self) -> float | None:
        """마지막 동기화 이후 경과 시간."""
        return self._sync_info.age_seconds

    def inc(self, amount: float = 1) -> None:
        """
        Increment the gauge value.

        Args:
            amount: Amount to increment (default: 1)
        """
        with self._lock:
            self._shadow_value += amount
            self._gauge_child.inc(amount)
            self._initialized = True
            self._sync_info.mark_synced("push")

    def dec(self, amount: float = 1) -> None:
        """
        Decrement the gauge value, clamping at 0.

        This is the key safety feature: if the shadow value would go
        negative, we set to 0 instead. This prevents the embarrassing
        "-1 pending items" display after server restarts.

        Args:
            amount: Amount to decrement (default: 1)
        """
        with self._lock:
            if not self._initialized:
                # First operation after restart is a dec - likely stale event
                # Don't decrement, just log and return
                logger.debug(
                    "safe_gauge.ignoring_dec_before_any",
                    label_values=self._label_values,
                )
                return

            if self._shadow_value >= amount:
                # Normal case: sufficient value to decrement
                self._shadow_value -= amount
                self._gauge_child.dec(amount)
            else:
                # Edge case: would go negative, clamp to 0
                old_value = self._shadow_value
                self._shadow_value = 0.0
                # Set to 0 instead of decrementing
                self._gauge_child.set(0)
                logger.warning(
                    "safe_gauge.clamped_gauge_indicate_event",
                    exceeded_decrement_value=old_value - amount,
                    label_values=self._label_values,
                )

            self._sync_info.mark_synced("push")

    def set(self, value: float, source: str = "manual") -> None:
        """
        Set the gauge to a specific value.

        Args:
            value: Value to set (clamped to 0 if negative)
            source: Sync source identifier (default: "manual")
        """
        with self._lock:
            if value < 0:
                logger.warning(
                    "safe_gauge.attempted_set_negative_value",
                    rejected_value=value,
                    label_values=self._label_values,
                )
                value = 0.0
            self._shadow_value = value
            self._gauge_child.set(value)
            self._initialized = True
            self._sync_info.mark_synced(source)

    def get_shadow_value(self) -> float:
        """
        Get the current shadow value (for testing/debugging).

        Returns:
            Current shadow counter value
        """
        with self._lock:
            return self._shadow_value

    def sync_from_source(self, actual_value: float, source: str = "reconciler") -> None:
        """
        Sync shadow value from authoritative source (Reconciler callback).

        Called by MetricReconciler to correct drift between
        in-memory shadow and actual DB state.

        Args:
            actual_value: Actual value from DB or external source
            source: Sync source identifier (e.g., "hydration", "manual", "snapshot")
        """
        with self._lock:
            if actual_value < 0:
                actual_value = 0.0
            old_shadow = self._shadow_value
            self._shadow_value = actual_value
            self._gauge_child.set(actual_value)
            self._initialized = True
            self._sync_info.mark_synced(source)
            if old_shadow != actual_value:
                logger.info(
                    "safe_gauge.synced_source",
                    old_shadow=old_shadow,
                    actual_value=actual_value,
                    label_values=self._label_values,
                )

    def mark_stale(self, reason: str = "external") -> None:
        """
        수동으로 stale 상태 마킹.

        Args:
            reason: Stale 이유
        """
        with self._lock:
            self._sync_info.mark_stale(reason)

    def get_reliability_info(self) -> dict[str, Any]:
        """
        메트릭 신뢰도 정보 반환.

        Returns:
            신뢰도 정보 딕셔너리
        """
        with self._lock:
            self._sync_info.check_staleness()
            return {
                "is_synced": self._sync_info.is_synced,
                "status": self._sync_info.status.value,
                "last_sync_time": self._sync_info.last_sync_time,
                "last_sync_source": self._sync_info.last_sync_source,
                "age_seconds": self._sync_info.age_seconds,
                "is_recovering": self._sync_info.is_recovering,
                "recovery_progress": self._sync_info.recovery_progress,
                "shadow_value": self._shadow_value,
                "labels": self._label_values,
            }


class SafeGauge:
    """
    Safe wrapper for Prometheus Gauge with LRU-based memory management.

    Wraps a Prometheus Gauge and returns SafeGaugeChild instances
    for labeled gauge operations, preventing negative values.

    This pattern is inspired by Netflix's metric handling approach:
    - Internal safety mechanisms (clamping)
    - External simplicity (standard Gauge interface)
    - Eventual consistency (Reconciler syncs periodically)

    Memory Management:
    - LRU 캐시로 레이블 조합 무한 증가 방지
    - max_label_combinations 초과 시 가장 오래된 레이블 조합 제거
    - Eviction 시 경고 로그 및 선택적 콜백 호출

    Example:
        >>> from prometheus_client import Gauge
        >>> raw = Gauge("dlq_pending", "Pending DLQ items", ["domain"])
        >>> safe = SafeGauge(raw, max_label_combinations=500)
        >>>
        >>> # Use like normal Gauge
        >>> safe.labels(domain="payment").inc()
        >>> safe.labels(domain="payment").dec()  # Won't go below 0

    Environment Settings:
        - 단일 서버: max_label_combinations=1000 (기본값)
        - K8s 10 Pods: max_label_combinations=500
        - K8s 100+ Pods: max_label_combinations=200
    """

    # 하위 호환성용 레거시 상수
    DEFAULT_MAX_LABEL_COMBINATIONS = 1000

    def __init__(
        self,
        gauge: Gauge | None,
        max_label_combinations: int | None = None,
        on_eviction: Callable[[tuple, SafeGaugeChild], None] | None = None,
    ):
        """
        Initialize SafeGauge with LRU cache.

        Args:
            gauge: Prometheus Gauge to wrap. If None, operations are no-ops.
            max_label_combinations: 캐시할 최대 레이블 조합 수. None이면 Settings에서 가져옴.
                                    초과 시 가장 오래된 조합 자동 제거.
            on_eviction: 레이블 조합 제거 시 호출되는 콜백 (모니터링용).
                        (evicted_key, evicted_child) -> None
        """
        self._gauge = gauge
        self._children: OrderedDict[tuple, SafeGaugeChild] = OrderedDict()
        self._max_label_combinations = (
            max_label_combinations
            if max_label_combinations is not None
            else _get_max_label_combinations()
        )
        self._on_eviction = on_eviction
        self._lock = threading.Lock()
        self._eviction_count = 0

    def labels(self, **kwargs) -> SafeGaugeChild:
        """
        Get a SafeGaugeChild for the given labels.

        LRU 캐시 사용: 최근 접근한 레이블 조합은 보존되고,
        max_label_combinations 초과 시 가장 오래된 조합 제거.

        Args:
            **kwargs: Label key-value pairs

        Returns:
            SafeGaugeChild instance for thread-safe operations
        """
        if self._gauge is None:
            # NoOpGaugeChild duck-types the SafeGaugeChild surface used by callers
            # (inc/dec/set/get_shadow_value/sync_from_source/mark_stale/etc.).
            return cast("SafeGaugeChild", NoOpGaugeChild())

        key = tuple(sorted(kwargs.items()))

        with self._lock:
            if key in self._children:
                # LRU: 최근 접근으로 이동
                self._children.move_to_end(key)
                return self._children[key]

            # 캐시 용량 초과 시 가장 오래된 항목 제거
            if len(self._children) >= self._max_label_combinations:
                self._evict_oldest()

            # 새 child 생성
            gauge_child = self._gauge.labels(**kwargs)
            child = SafeGaugeChild(gauge_child, kwargs)
            self._children[key] = child
            return child

    def _evict_oldest(self) -> None:
        """
        가장 오래된 레이블 조합 제거 (LRU eviction).

        제거된 조합의 shadow_value는 손실됩니다.
        경고 로그를 기록하고, on_eviction 콜백이 있으면 호출합니다.
        """
        if not self._children:
            return

        oldest_key, oldest_child = self._children.popitem(last=False)
        self._eviction_count += 1

        # 운영 인지를 위한 경고 로그
        logger.warning(
            "safe_gauge.lru_eviction",
            eviction_count=self._eviction_count,
            dict=dict(oldest_key),
            oldest_child=oldest_child.get_shadow_value(),
            max_label_combinations=self._max_label_combinations,
        )

        # Eviction 메트릭 기록 (prometheus가 있는 경우)
        try:
            from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE

            if PROMETHEUS_AVAILABLE:
                # 간단한 Counter로 기록 (별도 정의 필요 시 확장)
                pass  # 메트릭은 선택적, 로그만으로도 충분
        except ImportError:
            pass

        # 콜백 호출 (커스텀 처리용)
        if self._on_eviction:
            try:
                self._on_eviction(oldest_key, oldest_child)
            except Exception as e:
                logger.exception(
                    "safe_gauge.eviction_callback_failed",
                    error=e,
                )

    def get_child(self, **kwargs) -> SafeGaugeChild | None:
        """
        Get existing SafeGaugeChild without creating new one.

        LRU 순서는 업데이트하지 않음 (조회만).

        Args:
            **kwargs: Label key-value pairs

        Returns:
            SafeGaugeChild if exists, None otherwise
        """
        key = tuple(sorted(kwargs.items()))
        with self._lock:
            return self._children.get(key)

    @property
    def is_available(self) -> bool:
        """Check if underlying gauge is available."""
        return self._gauge is not None

    @property
    def current_size(self) -> int:
        """현재 캐시된 레이블 조합 수."""
        with self._lock:
            return len(self._children)

    @property
    def max_size(self) -> int:
        """최대 캐시 가능한 레이블 조합 수."""
        return self._max_label_combinations

    @property
    def eviction_count(self) -> int:
        """생성 이후 총 eviction 횟수."""
        return self._eviction_count

    def get_cache_stats(self) -> dict[str, Any]:
        """
        캐시 통계 정보 반환 (모니터링용).

        Returns:
            Dict with cache stats:
            - current_size: 현재 캐시 크기
            - max_size: 최대 캐시 크기
            - eviction_count: 총 eviction 횟수
            - utilization_percent: 캐시 사용률 (%)
        """
        with self._lock:
            current = len(self._children)
            return {
                "current_size": current,
                "max_size": self._max_label_combinations,
                "eviction_count": self._eviction_count,
                "utilization_percent": (
                    (current / self._max_label_combinations) * 100
                    if self._max_label_combinations > 0
                    else 0
                ),
            }


__all__ = [
    "SafeGauge",
    "SafeGaugeChild",
]
