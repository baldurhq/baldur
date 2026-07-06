"""
Throttle Prometheus 메트릭 테스트.

테스트 대상:
1. throttle_current_limit (Gauge)
2. throttle_rtt_ms (Histogram)
3. throttle_gradient (Gauge)
4. throttle_denied_total (Counter)
5. throttle_emergency_adjustments_total (Counter)
6. throttle_cb_adjustments_total (Counter)
"""


class TestThrottleCurrentLimitMetric:
    """throttle_current_limit 메트릭 테스트."""

    def test_register_throttle_current_limit(self):
        """throttle_current_limit 메트릭 등록 테스트."""
        from baldur.metrics.registry import get_or_create_gauge

        # definitions 모듈 리로드하면 메트릭이 등록됨
        # 등록이 예외 없이 완료되는지 확인
        metric = get_or_create_gauge(
            "throttle_current_limit",
            "현재 적용 중인 스로틀 한도",
            ["service"],
        )
        assert metric is not None

    def test_throttle_current_limit_with_label(self):
        """throttle_current_limit 메트릭 라벨 설정 테스트."""
        from baldur.metrics.registry import get_or_create_gauge

        metric = get_or_create_gauge(
            "throttle_current_limit",
            "현재 적용 중인 스로틀 한도",
            ["service"],
        )
        # 라벨로 메트릭 사용 가능한지 확인
        labeled = metric.labels(service="test-service")
        assert labeled is not None


class TestThrottleRttMsMetric:
    """throttle_rtt_ms 메트릭 테스트."""

    def test_register_throttle_rtt_ms(self):
        """throttle_rtt_ms 메트릭 등록 테스트."""
        from baldur.metrics.registry import get_or_create_histogram

        metric = get_or_create_histogram(
            "throttle_rtt_ms",
            "응답 시간 분포 (밀리초)",
            ["service"],
            buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
        )
        assert metric is not None

    def test_throttle_rtt_ms_observe(self):
        """throttle_rtt_ms 메트릭 observe 테스트."""
        from baldur.metrics.registry import get_or_create_histogram

        metric = get_or_create_histogram(
            "throttle_rtt_ms",
            "응답 시간 분포 (밀리초)",
            ["service"],
            buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
        )
        labeled = metric.labels(service="test-service")
        # observe 호출이 예외 없이 완료되는지 확인
        labeled.observe(150.0)


class TestThrottleGradientMetric:
    """throttle_gradient 메트릭 테스트."""

    def test_register_throttle_gradient(self):
        """throttle_gradient 메트릭 등록 테스트."""
        from baldur.metrics.registry import get_or_create_gauge

        metric = get_or_create_gauge(
            "throttle_gradient",
            "현재 그래디언트 값",
            ["service"],
        )
        assert metric is not None


class TestThrottleDeniedTotalMetric:
    """throttle_denied_total 메트릭 테스트."""

    def test_register_throttle_denied_total(self):
        """throttle_denied_total 메트릭 등록 테스트."""
        from baldur.metrics.registry import get_or_create_counter

        metric = get_or_create_counter(
            "throttle_denied_total",
            "스로틀로 인해 거부된 총 요청 수",
            ["service", "reason"],
        )
        assert metric is not None

    def test_throttle_denied_total_with_reason_label(self):
        """throttle_denied_total 메트릭 reason 라벨 테스트."""
        from baldur.metrics.registry import get_or_create_counter

        metric = get_or_create_counter(
            "throttle_denied_total",
            "스로틀로 인해 거부된 총 요청 수",
            ["service", "reason"],
        )
        labeled = metric.labels(service="test-service", reason="limit_exceeded")
        assert labeled is not None


class TestThrottleEmergencyAdjustmentsMetric:
    """throttle_emergency_adjustments_total 메트릭 테스트."""

    def test_register_emergency_adjustments(self):
        """throttle_emergency_adjustments_total 메트릭 등록 테스트."""
        from baldur.metrics.registry import get_or_create_counter

        metric = get_or_create_counter(
            "throttle_emergency_adjustments_total",
            "Emergency Level 변경으로 인한 한도 조정 횟수",
            ["level"],
        )
        assert metric is not None

    def test_emergency_adjustments_with_level_label(self):
        """throttle_emergency_adjustments_total 메트릭 level 라벨 테스트."""
        from baldur.metrics.registry import get_or_create_counter

        metric = get_or_create_counter(
            "throttle_emergency_adjustments_total",
            "Emergency Level 변경으로 인한 한도 조정 횟수",
            ["level"],
        )
        labeled = metric.labels(level="2")
        assert labeled is not None


class TestThrottleCbAdjustmentsMetric:
    """throttle_cb_adjustments_total 메트릭 테스트."""

    def test_register_cb_adjustments(self):
        """throttle_cb_adjustments_total 메트릭 등록 테스트."""
        from baldur.metrics.registry import get_or_create_counter

        metric = get_or_create_counter(
            "throttle_cb_adjustments_total",
            "CB 상태 변경으로 인한 한도 조정 횟수",
            ["service", "cb_state"],
        )
        assert metric is not None

    def test_cb_adjustments_with_state_label(self):
        """throttle_cb_adjustments_total 메트릭 cb_state 라벨 테스트."""
        from baldur.metrics.registry import get_or_create_counter

        metric = get_or_create_counter(
            "throttle_cb_adjustments_total",
            "CB 상태 변경으로 인한 한도 조정 횟수",
            ["service", "cb_state"],
        )
        labeled = metric.labels(service="test-service", cb_state="OPEN")
        assert labeled is not None
