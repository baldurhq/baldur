"""
Leader Election Prometheus Metrics.

리더 선출 상태 모니터링을 위한 Prometheus 메트릭.
"""

from __future__ import annotations

import time

import structlog

from baldur.metrics._metric_protocol import (
    CounterMetric,
    GaugeMetric,
    HistogramMetric,
)

logger = structlog.get_logger()


LEADER_ELECTOR_IS_LEADER: GaugeMetric
LEADER_ELECTOR_LEASE_EXPIRE_TIMESTAMP: GaugeMetric
LEADER_ELECTOR_RENEW_ERRORS_TOTAL: CounterMetric
LEADER_ELECTOR_ELECTIONS_TOTAL: CounterMetric
LEADER_ELECTOR_LEADERSHIP_DURATION_SECONDS: HistogramMetric


# Prometheus 클라이언트 가용성 확인
try:
    from baldur.metrics.registry import (
        get_or_create_counter,
        get_or_create_gauge,
        get_or_create_histogram,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

    # Dummy metric class (when prometheus_client is not installed)
    class DummyMetric:
        """Dummy metric used when prometheus_client is not available."""

        def labels(self, *args, **kwargs):
            return self

        def inc(self, amount=1):
            pass

        def set(self, value):
            pass

        def observe(self, value):
            pass


# =============================================================================
# Prometheus Metrics 정의
# =============================================================================

if PROMETHEUS_AVAILABLE:
    # Leader status (1 = leader, 0 = follower)
    LEADER_ELECTOR_IS_LEADER = get_or_create_gauge(
        "baldur_leader_elector_is_leader",
        "Whether the current node is leader (1=leader, 0=follower)",
        ["resource_name", "node_id"],
    )

    # Lease expiry timestamp (Unix epoch)
    LEADER_ELECTOR_LEASE_EXPIRE_TIMESTAMP = get_or_create_gauge(
        "baldur_leader_elector_lease_expire_timestamp",
        "Current lease expiry Unix timestamp",
        ["resource_name"],
    )

    # Lease renewal failure count
    LEADER_ELECTOR_RENEW_ERRORS_TOTAL = get_or_create_counter(
        "baldur_leader_elector_renew_errors_total",
        "Total lease renewal failures",
        ["resource_name", "error_type"],
    )

    # Leader election count
    LEADER_ELECTOR_ELECTIONS_TOTAL = get_or_create_counter(
        "baldur_leader_elector_elections_total",
        "Total leader elections",
        ["resource_name", "node_id"],
    )

    # Leadership duration (Histogram)
    LEADER_ELECTOR_LEADERSHIP_DURATION_SECONDS = get_or_create_histogram(
        "baldur_leader_elector_leadership_duration_seconds",
        "Leadership duration in seconds",
        ["resource_name"],
        buckets=(10, 30, 60, 300, 600, 1800, 3600, 7200),
    )
else:
    LEADER_ELECTOR_IS_LEADER = DummyMetric()
    LEADER_ELECTOR_LEASE_EXPIRE_TIMESTAMP = DummyMetric()
    LEADER_ELECTOR_RENEW_ERRORS_TOTAL = DummyMetric()
    LEADER_ELECTOR_ELECTIONS_TOTAL = DummyMetric()
    LEADER_ELECTOR_LEADERSHIP_DURATION_SECONDS = DummyMetric()


# =============================================================================
# Metrics Helper Class
# =============================================================================


class LeaderElectorMetrics:
    """
    Leader Elector 메트릭 헬퍼.

    LeaderElector에서 사용하는 메트릭 업데이트를 캡슐화.
    """

    def __init__(self, resource_name: str, node_id: str):
        """
        메트릭 헬퍼 초기화.

        Args:
            resource_name: 리소스 이름
            node_id: 노드 ID
        """
        self._resource_name = resource_name
        self._node_id = node_id
        self._leadership_start_time: float | None = None

    def set_leader(self, is_leader: bool) -> None:
        """리더 상태 설정."""
        LEADER_ELECTOR_IS_LEADER.labels(
            resource_name=self._resource_name,
            node_id=self._node_id,
        ).set(1 if is_leader else 0)

    def set_lease_expire_timestamp(self, expire_timestamp: float) -> None:
        """Lease 만료 시간 설정."""
        LEADER_ELECTOR_LEASE_EXPIRE_TIMESTAMP.labels(
            resource_name=self._resource_name,
        ).set(expire_timestamp)

    def record_renew_error(self, error_type: str = "unknown") -> None:
        """Lease 갱신 실패 기록."""
        LEADER_ELECTOR_RENEW_ERRORS_TOTAL.labels(
            resource_name=self._resource_name,
            error_type=error_type,
        ).inc()

    def record_election(self) -> None:
        """리더 선출 기록."""
        LEADER_ELECTOR_ELECTIONS_TOTAL.labels(
            resource_name=self._resource_name,
            node_id=self._node_id,
        ).inc()
        self._leadership_start_time = time.time()

    def record_leadership_end(self) -> None:
        """리더십 종료 기록 (유지 시간 측정)."""
        if self._leadership_start_time is not None:
            duration = time.time() - self._leadership_start_time
            LEADER_ELECTOR_LEADERSHIP_DURATION_SECONDS.labels(
                resource_name=self._resource_name,
            ).observe(duration)
            self._leadership_start_time = None
