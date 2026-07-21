"""
PropagationHealthMonitor Unit Tests.

"""


class TestPropagationHealthMetrics:
    """PropagationHealthMetrics 테스트."""

    def test_default_values(self):
        """기본값 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMetrics,
        )

        metrics = PropagationHealthMetrics()

        assert metrics.last_propagation_latency_ms == 0.0
        assert metrics.avg_propagation_latency_ms == 0.0
        assert metrics.tier1_sla_violations == 0
        assert metrics.tier2_sla_violations == 0
        assert metrics.total_propagations == 0
        assert metrics.propagation_health_score == 100.0
        assert metrics.calculated_at is not None

    def test_to_dict(self):
        """to_dict 변환 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMetrics,
        )

        metrics = PropagationHealthMetrics(
            last_propagation_latency_ms=150.0,
            avg_propagation_latency_ms=120.0,
            tier1_sla_violations=2,
            total_propagations=100,
            propagation_health_score=90.0,
        )

        data = metrics.to_dict()

        assert data["last_propagation_latency_ms"] == 150.0
        assert data["avg_propagation_latency_ms"] == 120.0
        assert data["tier1_sla_violations"] == 2
        assert data["total_propagations"] == 100
        assert data["propagation_health_score"] == 90.0


class TestPropagationHealthMonitor:
    """PropagationHealthMonitor 테스트."""

    def setup_method(self):
        """각 테스트 전에 싱글톤 리셋."""
        from baldur.services.config.propagation_health import (
            reset_propagation_health_monitor,
        )

        reset_propagation_health_monitor()

    def teardown_method(self):
        """각 테스트 후에 싱글톤 리셋."""
        from baldur.services.config.propagation_health import (
            reset_propagation_health_monitor,
        )

        reset_propagation_health_monitor()

    def test_record_propagation_basic(self):
        """기본 전파 기록 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.services.config.propagator import PropagationTier

        monitor = PropagationHealthMonitor()

        monitor.record_propagation(
            config_type="circuit_breaker",
            latency_ms=150.0,
            tier=PropagationTier.TIER_1_IMMEDIATE,
            source_cluster="seoul",
            target_cluster="tokyo",
        )

        metrics = monitor.get_current_metrics()

        assert metrics.total_propagations == 1
        assert metrics.last_propagation_latency_ms == 150.0
        assert metrics.avg_propagation_latency_ms == 150.0
        assert metrics.tier1_sla_violations == 0  # 150ms < 1000ms

    def test_tier1_sla_violation(self):
        """Tier 1 SLA 위반 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.services.config.propagator import PropagationTier

        monitor = PropagationHealthMonitor()

        # 1500ms > 1000ms threshold
        monitor.record_propagation(
            config_type="emergency",
            latency_ms=1500.0,
            tier=PropagationTier.TIER_1_IMMEDIATE,
            source_cluster="seoul",
            target_cluster="tokyo",
        )

        metrics = monitor.get_current_metrics()

        assert metrics.tier1_sla_violations == 1
        assert metrics.propagation_health_score == 95.0  # 100 - 5

    def test_tier2_sla_violation(self):
        """Tier 2 SLA 위반 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.services.config.propagator import PropagationTier

        monitor = PropagationHealthMonitor()

        # 35000ms > 30000ms threshold
        monitor.record_propagation(
            config_type="metrics",
            latency_ms=35000.0,
            tier=PropagationTier.TIER_2_EVENTUAL,
            source_cluster="seoul",
            target_cluster="tokyo",
        )

        metrics = monitor.get_current_metrics()

        assert metrics.tier2_sla_violations == 1
        assert metrics.propagation_health_score == 99.0  # 100 - 1

    def test_multiple_violations(self):
        """다중 위반 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.services.config.propagator import PropagationTier

        monitor = PropagationHealthMonitor()

        # 2 Tier 1 violations (-10) + 3 Tier 2 violations (-3) = 87
        for _ in range(2):
            monitor.record_propagation(
                config_type="emergency",
                latency_ms=1500.0,
                tier=PropagationTier.TIER_1_IMMEDIATE,
                source_cluster="seoul",
                target_cluster="tokyo",
            )

        for _ in range(3):
            monitor.record_propagation(
                config_type="metrics",
                latency_ms=35000.0,
                tier=PropagationTier.TIER_2_EVENTUAL,
                source_cluster="seoul",
                target_cluster="tokyo",
            )

        metrics = monitor.get_current_metrics()

        assert metrics.tier1_sla_violations == 2
        assert metrics.tier2_sla_violations == 3
        assert metrics.propagation_health_score == 87.0

    def test_get_combined_health_score(self):
        """종합 HealthScore 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.services.config.propagator import PropagationTier

        monitor = PropagationHealthMonitor()

        # 2 Tier 1 violations = 90 propagation score
        for _ in range(2):
            monitor.record_propagation(
                config_type="emergency",
                latency_ms=1500.0,
                tier=PropagationTier.TIER_1_IMMEDIATE,
                source_cluster="seoul",
                target_cluster="tokyo",
            )

        # integrity_score=100, propagation_weight=0.3
        # = 100*0.7 + 90*0.3 = 70 + 27 = 97
        combined = monitor.get_combined_health_score(
            integrity_score=100.0,
            propagation_weight=0.3,
        )

        assert combined == 97.0

    def test_percentile_calculation(self):
        """백분위 계산 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.services.config.propagator import PropagationTier

        monitor = PropagationHealthMonitor()

        # 다양한 레이턴시 기록
        latencies = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        for lat in latencies:
            monitor.record_propagation(
                config_type="test",
                latency_ms=float(lat),
                tier=PropagationTier.TIER_2_EVENTUAL,
                source_cluster="seoul",
                target_cluster="tokyo",
            )

        metrics = monitor.get_current_metrics()

        assert metrics.total_propagations == 10
        assert metrics.avg_propagation_latency_ms == 550.0
        assert (
            metrics.p50_propagation_latency_ms == 600.0
        )  # 50th percentile (index 5 in 0-indexed array)

    def test_get_recent_records(self):
        """최근 기록 조회 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.services.config.propagator import PropagationTier

        monitor = PropagationHealthMonitor()

        for i in range(5):
            monitor.record_propagation(
                config_type=f"type_{i}",
                latency_ms=float(i * 100),
                tier=PropagationTier.TIER_2_EVENTUAL,
                source_cluster="seoul",
                target_cluster="tokyo",
            )

        records = monitor.get_recent_records(count=3)

        assert len(records) == 3
        assert records[0]["config_type"] == "type_2"
        assert records[2]["config_type"] == "type_4"

    def test_reset(self):
        """리셋 테스트."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.services.config.propagator import PropagationTier

        monitor = PropagationHealthMonitor()

        monitor.record_propagation(
            config_type="test",
            latency_ms=1500.0,
            tier=PropagationTier.TIER_1_IMMEDIATE,
            source_cluster="seoul",
            target_cluster="tokyo",
        )

        assert monitor.get_current_metrics().total_propagations == 1

        monitor.reset()

        assert monitor.get_current_metrics().total_propagations == 0

    def test_singleton(self):
        """싱글톤 패턴 테스트."""
        from baldur.services.config.propagation_health import (
            get_propagation_health_monitor,
            reset_propagation_health_monitor,
        )

        reset_propagation_health_monitor()

        m1 = get_propagation_health_monitor()
        m2 = get_propagation_health_monitor()

        assert m1 is m2
