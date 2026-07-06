"""
StuckDetector 테스트.

Zero-variance 기반 Stuck 감지 테스트.
"""

import pytest

from baldur.meta.stuck_detector import (
    MetricSample,
    MetricWindow,
    StuckDetectionResult,
    StuckDetector,
    get_stuck_detector,
    reset_stuck_detector,
)


class TestMetricSample:
    """MetricSample 데이터클래스 테스트."""

    def test_creation(self):
        """생성 테스트."""
        sample = MetricSample(value=100.0, timestamp=1234567890.0, error=False)

        assert sample.value == 100.0
        assert sample.timestamp == 1234567890.0
        assert sample.error is False

    def test_error_sample(self):
        """에러 샘플 생성 테스트."""
        sample = MetricSample(value=50.0, timestamp=1234567890.0, error=True)

        assert sample.error is True


class TestMetricWindow:
    """MetricWindow 테스트."""

    def test_add_sample(self):
        """샘플 추가 테스트."""
        window = MetricWindow(max_size=10)

        window.add(100.0, error=False)
        window.add(200.0, error=False)

        assert len(window.samples) == 2

    def test_max_size_limit(self):
        """최대 크기 제한 테스트."""
        window = MetricWindow(max_size=5)

        for i in range(10):
            window.add(float(i), error=False)

        assert len(window.samples) == 5

    def test_variance_constant_values(self):
        """상수값 분산 테스트 (분산 ≈ 0)."""
        window = MetricWindow(max_size=10)

        for _ in range(10):
            window.add(100.0, error=False)

        assert window.variance() < 0.001

    def test_variance_varying_values(self):
        """변동값 분산 테스트."""
        window = MetricWindow(max_size=10)

        for i in range(10):
            window.add(float(i * 10), error=False)

        assert window.variance() > 0.1

    def test_variance_insufficient_samples(self):
        """샘플 부족 시 무한대 분산."""
        window = MetricWindow(max_size=10)

        window.add(100.0, error=False)

        assert window.variance() == float("inf")

    def test_error_rate_calculation(self):
        """에러율 계산 테스트."""
        window = MetricWindow(max_size=10)

        for i in range(10):
            window.add(100.0, error=(i % 2 == 0))  # 50% 에러

        assert 0.45 <= window.error_rate() <= 0.55

    def test_error_rate_empty(self):
        """빈 윈도우 에러율 테스트."""
        window = MetricWindow(max_size=10)

        assert window.error_rate() == 0.0

    def test_is_stuck_true(self):
        """Stuck 상태 감지 테스트."""
        window = MetricWindow(max_size=10)

        # 상수값 + 높은 에러율
        for _i in range(10):
            window.add(100.0, error=True)  # 모든 샘플이 에러

        assert window.is_stuck(variance_threshold=0.001, error_rate_threshold=0.5)

    def test_is_stuck_false_varying_values(self):
        """변동값은 Stuck이 아님."""
        window = MetricWindow(max_size=10)

        for i in range(10):
            window.add(float(i * 10), error=True)

        assert (
            window.is_stuck(variance_threshold=0.001, error_rate_threshold=0.5) is False
        )

    def test_is_stuck_false_low_error_rate(self):
        """낮은 에러율은 Stuck이 아님."""
        window = MetricWindow(max_size=10)

        for _ in range(10):
            window.add(100.0, error=False)

        assert (
            window.is_stuck(variance_threshold=0.001, error_rate_threshold=0.5) is False
        )

    def test_is_stuck_insufficient_samples(self):
        """샘플 부족 시 Stuck 아님."""
        window = MetricWindow(max_size=10)

        for _ in range(3):
            window.add(100.0, error=True)

        assert window.is_stuck() is False

    def test_mean_calculation(self):
        """평균 계산 테스트."""
        window = MetricWindow(max_size=10)

        for i in range(1, 11):
            window.add(float(i), error=False)

        assert window.mean() == 5.5

    def test_clear(self):
        """초기화 테스트."""
        window = MetricWindow(max_size=10)

        for _ in range(10):
            window.add(100.0, error=False)

        window.clear()

        assert len(window.samples) == 0


class TestStuckDetectionResult:
    """StuckDetectionResult 데이터클래스 테스트."""

    def test_creation(self):
        """생성 테스트."""
        result = StuckDetectionResult(
            component="dlq",
            is_stuck=True,
            variance=0.0001,
            error_rate=0.8,
            sample_count=20,
            duration_seconds=60.0,
            mean_value=1000.0,
        )

        assert result.component == "dlq"
        assert result.is_stuck is True
        assert result.variance < 0.001


class TestStuckDetector:
    """StuckDetector 테스트."""

    @pytest.fixture
    def detector(self):
        """Detector fixture."""
        return StuckDetector(
            window_size=10,
            variance_threshold=0.001,
            error_rate_threshold=0.5,
        )

    def test_record_creates_window(self, detector):
        """기록 시 윈도우 생성 테스트."""
        detector.record("dlq", 100.0, error=False)

        assert "dlq" in detector.get_component_names()

    def test_check_empty_component(self, detector):
        """미등록 컴포넌트 체크 테스트."""
        result = detector.check("unknown")

        assert result.is_stuck is False
        assert result.sample_count == 0

    def test_check_stuck_detection(self, detector):
        """Stuck 감지 테스트."""
        # 상수값 + 모든 에러
        for _ in range(10):
            detector.record("dlq", 1000.0, error=True)

        result = detector.check("dlq")

        assert result.is_stuck is True
        assert result.variance < 0.001
        assert result.error_rate == 1.0

    def test_check_not_stuck(self, detector):
        """정상 상태 테스트."""
        for i in range(10):
            detector.record("dlq", float(i * 100), error=False)

        result = detector.check("dlq")

        assert result.is_stuck is False

    def test_check_all(self, detector):
        """모든 컴포넌트 체크 테스트."""
        detector.record("dlq", 100.0, error=False)
        detector.record("redis", 200.0, error=False)
        detector.record("cb", 300.0, error=False)

        results = detector.check_all()

        assert len(results) == 3
        assert "dlq" in results
        assert "redis" in results
        assert "cb" in results

    def test_get_stuck_components(self, detector):
        """Stuck 컴포넌트 목록 테스트."""
        # dlq: Stuck
        for _ in range(10):
            detector.record("dlq", 1000.0, error=True)

        # redis: 정상
        for i in range(10):
            detector.record("redis", float(i * 100), error=False)

        stuck = detector.get_stuck_components()

        assert "dlq" in stuck
        assert "redis" not in stuck

    def test_clear_specific_component(self, detector):
        """특정 컴포넌트 초기화 테스트."""
        detector.record("dlq", 100.0, error=False)
        detector.record("redis", 200.0, error=False)

        detector.clear("dlq")

        components = detector.get_component_names()
        assert "dlq" not in components
        assert "redis" in components

    def test_clear_all(self, detector):
        """전체 초기화 테스트."""
        detector.record("dlq", 100.0, error=False)
        detector.record("redis", 200.0, error=False)

        detector.clear()

        assert len(detector.get_component_names()) == 0


class TestStuckDetectorSingleton:
    """싱글톤 테스트."""

    def test_singleton_returns_same_instance(self):
        """싱글톤 인스턴스 반환 테스트."""
        reset_stuck_detector()

        detector1 = get_stuck_detector()
        detector2 = get_stuck_detector()

        assert detector1 is detector2

        reset_stuck_detector()

    def test_reset_clears_singleton(self):
        """싱글톤 리셋 테스트."""
        reset_stuck_detector()

        detector1 = get_stuck_detector()
        reset_stuck_detector()
        detector2 = get_stuck_detector()

        assert detector1 is not detector2

        reset_stuck_detector()
