"""
Unit tests for BackpressureMetrics.

테스트 항목:
- 메트릭 초기화 (prometheus_client 유무)
- 메트릭 설정/증가 메서드
- 메트릭 비활성화 시 no-op
"""

import pytest

from baldur.scaling.config import (
    BackpressureSettings,
    reset_backpressure_settings,
)
from baldur.scaling.metrics import (
    HAS_PROMETHEUS,
    BackpressureMetrics,
    get_backpressure_metrics,
)

# prometheus_client 레지스트리 리셋을 위한 import
if HAS_PROMETHEUS:
    from prometheus_client import REGISTRY


def _clean_prometheus_registry():
    """Prometheus 레지스트리에서 baldur_ 메트릭 정리."""
    if not HAS_PROMETHEUS:
        return
    collectors_to_remove = []
    for collector in list(REGISTRY._collector_to_names.keys()):
        names = REGISTRY._collector_to_names.get(collector, [])
        if any(
            name.startswith("baldur_") or name.startswith("custom_") for name in names
        ):
            collectors_to_remove.append(collector)
    for collector in collectors_to_remove:
        try:
            REGISTRY.unregister(collector)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def reset_prometheus_registry():
    """Prometheus 레지스트리 리셋 (테스트 전후)."""
    _clean_prometheus_registry()
    yield
    _clean_prometheus_registry()


class TestBackpressureMetrics:
    """BackpressureMetrics 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, reset_prometheus_registry):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_backpressure_settings()
        import baldur.scaling.metrics as metrics_module

        metrics_module._metrics = None
        yield
        reset_backpressure_settings()
        metrics_module._metrics = None

    def test_initialization_with_default_settings(self):
        """기본 설정으로 초기화."""
        metrics = BackpressureMetrics()
        assert metrics._prefix == "baldur_"

    def test_initialization_with_custom_settings(self):
        """커스텀 설정으로 초기화."""
        settings = BackpressureSettings(
            metrics_prefix="custom_",
            metrics_enabled=True,
        )
        metrics = BackpressureMetrics(settings=settings)
        assert metrics._prefix == "custom_"

    @pytest.mark.skipif(not HAS_PROMETHEUS, reason="prometheus_client not installed")
    def test_set_queue_depth(self):
        """큐 깊이 설정 테스트."""
        settings = BackpressureSettings(metrics_enabled=True)
        metrics = BackpressureMetrics(settings=settings)

        # 예외 없이 실행되는지 확인
        metrics.set_queue_depth("test_queue", 100)

    @pytest.mark.skipif(not HAS_PROMETHEUS, reason="prometheus_client not installed")
    def test_set_processing_rate(self):
        """처리율 설정 테스트."""
        settings = BackpressureSettings(metrics_enabled=True)
        metrics = BackpressureMetrics(settings=settings)

        metrics.set_processing_rate("worker", 500.0)

    @pytest.mark.skipif(not HAS_PROMETHEUS, reason="prometheus_client not installed")
    def test_set_backpressure_level(self):
        """Backpressure 레벨 설정 테스트."""
        settings = BackpressureSettings(metrics_enabled=True)
        metrics = BackpressureMetrics(settings=settings)

        metrics.set_backpressure_level("worker", 2)

    @pytest.mark.skipif(not HAS_PROMETHEUS, reason="prometheus_client not installed")
    def test_inc_processed(self):
        """처리 카운터 증가 테스트."""
        settings = BackpressureSettings(metrics_enabled=True)
        metrics = BackpressureMetrics(settings=settings)

        metrics.inc_processed("worker", "success")

    @pytest.mark.skipif(not HAS_PROMETHEUS, reason="prometheus_client not installed")
    def test_inc_dropped(self):
        """드롭 카운터 증가 테스트."""
        settings = BackpressureSettings(metrics_enabled=True)
        metrics = BackpressureMetrics(settings=settings)

        metrics.inc_dropped("worker", "backpressure")

    @pytest.mark.skipif(not HAS_PROMETHEUS, reason="prometheus_client not installed")
    def test_observe_duration(self):
        """처리 시간 기록 테스트."""
        settings = BackpressureSettings(metrics_enabled=True)
        metrics = BackpressureMetrics(settings=settings)

        metrics.observe_duration("worker", "process", 0.5)


class TestBackpressureMetricsDisabled:
    """메트릭 비활성화 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, reset_prometheus_registry):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_backpressure_settings()
        import baldur.scaling.metrics as metrics_module

        metrics_module._metrics = None
        yield
        reset_backpressure_settings()
        metrics_module._metrics = None

    def test_methods_are_noop_when_disabled(self):
        """비활성화 시 메서드가 no-op."""
        settings = BackpressureSettings(metrics_enabled=False)
        metrics = BackpressureMetrics(settings=settings)

        # 예외 없이 실행되어야 함
        metrics.set_queue_depth("test", 100)
        metrics.set_processing_rate("worker", 500.0)
        metrics.set_backpressure_level("worker", 2)
        metrics.inc_processed("worker")
        metrics.inc_dropped("worker")
        metrics.observe_duration("worker", "op", 0.1)


class TestBackpressureMetricsSingleton:
    """BackpressureMetrics 싱글톤 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self, reset_prometheus_registry):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_backpressure_settings()
        import baldur.scaling.metrics as metrics_module

        metrics_module._metrics = None
        yield
        reset_backpressure_settings()
        metrics_module._metrics = None

    def test_singleton_returns_same_instance(self):
        """싱글톤이 동일한 인스턴스를 반환하는지 확인."""
        metrics1 = get_backpressure_metrics()
        metrics2 = get_backpressure_metrics()
        assert metrics1 is metrics2
