"""
Leader Elector Prometheus 메트릭 테스트.
"""

import time

from baldur.coordination.metrics import (
    PROMETHEUS_AVAILABLE,
    LeaderElectorMetrics,
)


class TestLeaderElectorMetrics:
    """LeaderElectorMetrics 테스트."""

    def test_init(self):
        """메트릭 헬퍼 초기화 테스트."""
        metrics = LeaderElectorMetrics(
            resource_name="test-resource",
            node_id="test-node",
        )

        assert metrics._resource_name == "test-resource"
        assert metrics._node_id == "test-node"
        assert metrics._leadership_start_time is None

    def test_set_leader_true(self):
        """리더 상태 설정 (True) 테스트."""
        metrics = LeaderElectorMetrics(
            resource_name="test-resource",
            node_id="test-node",
        )

        # 예외 없이 실행되어야 함
        metrics.set_leader(True)

    def test_set_leader_false(self):
        """리더 상태 설정 (False) 테스트."""
        metrics = LeaderElectorMetrics(
            resource_name="test-resource",
            node_id="test-node",
        )

        metrics.set_leader(False)

    def test_set_lease_expire_timestamp(self):
        """Lease 만료 시간 설정 테스트."""
        metrics = LeaderElectorMetrics(
            resource_name="test-resource",
            node_id="test-node",
        )

        expire_ts = time.time() + 30
        metrics.set_lease_expire_timestamp(expire_ts)

    def test_record_renew_error(self):
        """갱신 오류 기록 테스트."""
        metrics = LeaderElectorMetrics(
            resource_name="test-resource",
            node_id="test-node",
        )

        metrics.record_renew_error("connection_error")
        metrics.record_renew_error()  # 기본값 unknown

    def test_record_election(self):
        """선출 기록 테스트."""
        metrics = LeaderElectorMetrics(
            resource_name="test-resource",
            node_id="test-node",
        )

        assert metrics._leadership_start_time is None
        metrics.record_election()
        assert metrics._leadership_start_time is not None

    def test_record_leadership_end(self):
        """리더십 종료 기록 테스트."""
        metrics = LeaderElectorMetrics(
            resource_name="test-resource",
            node_id="test-node",
        )

        # 선출 기록 없이 종료 기록 (아무것도 안 함)
        metrics.record_leadership_end()
        assert metrics._leadership_start_time is None

        # 선출 후 종료
        metrics.record_election()
        time.sleep(0.01)  # 최소 시간 경과
        metrics.record_leadership_end()
        assert metrics._leadership_start_time is None


class TestPrometheusAvailability:
    """Prometheus 가용성 테스트."""

    def test_prometheus_available_check(self):
        """PROMETHEUS_AVAILABLE 플래그 확인."""
        # prometheus_client가 설치되어 있으면 True, 아니면 False
        assert isinstance(PROMETHEUS_AVAILABLE, bool)
