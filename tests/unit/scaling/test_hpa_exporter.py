"""
HPAMetricsExporter 단위 테스트.

테스트 항목:
- HPAMetricsExporter 초기화
- start/stop 동작
- 메트릭 업데이트
- 싱글톤 패턴
- LEVEL_TO_INT 매핑
"""

from unittest.mock import MagicMock

import pytest

from baldur.scaling.config import BackpressureLevel, BackpressureSettings
from baldur.scaling.hpa_exporter import (
    LEVEL_TO_INT,
    HPAMetricsExporter,
    get_hpa_metrics_exporter,
    reset_hpa_metrics_exporter,
)
from baldur.scaling.rate_controller import RateControllerState


class TestLevelToInt:
    """LEVEL_TO_INT 매핑 테스트."""

    def test_all_levels_mapped(self):
        """모든 BackpressureLevel이 매핑되어 있는지 확인."""
        for level in BackpressureLevel:
            assert level in LEVEL_TO_INT

    def test_level_values(self):
        """레벨 값 확인."""
        assert LEVEL_TO_INT[BackpressureLevel.NONE] == 0
        assert LEVEL_TO_INT[BackpressureLevel.LOW] == 1
        assert LEVEL_TO_INT[BackpressureLevel.MEDIUM] == 2
        assert LEVEL_TO_INT[BackpressureLevel.HIGH] == 3
        assert LEVEL_TO_INT[BackpressureLevel.CRITICAL] == 4


class TestHPAMetricsExporter:
    """HPAMetricsExporter 테스트."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """각 테스트 전 싱글톤 리셋."""
        reset_hpa_metrics_exporter()
        yield
        reset_hpa_metrics_exporter()

    def test_init_with_defaults(self):
        """기본값으로 초기화 확인."""
        exporter = HPAMetricsExporter()

        assert exporter._component_name == HPAMetricsExporter.DEFAULT_COMPONENT_NAME
        assert exporter._queue_name == HPAMetricsExporter.DEFAULT_QUEUE_NAME
        assert exporter._update_interval == HPAMetricsExporter.DEFAULT_UPDATE_INTERVAL
        assert not exporter.is_running()

    def test_init_with_custom_values(self):
        """커스텀 값으로 초기화 확인."""

        def queue_provider():
            return 100

        exporter = HPAMetricsExporter(
            queue_size_provider=queue_provider,
            component_name="my_component",
            queue_name="my_queue",
            update_interval=10.0,
        )

        assert exporter._component_name == "my_component"
        assert exporter._queue_name == "my_queue"
        assert exporter._update_interval == 10.0

    def test_start_and_stop(self):
        """start/stop 동작 확인."""
        settings = BackpressureSettings(
            hpa_enabled=True,
            metrics_enabled=True,
        )
        exporter = HPAMetricsExporter(settings=settings)

        # Start
        exporter.start()
        assert exporter.is_running()

        # Stop
        exporter.stop()
        assert not exporter.is_running()

    def test_start_when_hpa_disabled(self):
        """HPA 비활성화 시 시작하지 않음 확인."""
        settings = BackpressureSettings(hpa_enabled=False)
        exporter = HPAMetricsExporter(settings=settings)

        exporter.start()
        assert not exporter.is_running()

    def test_start_when_metrics_disabled(self):
        """메트릭 비활성화 시 시작하지 않음 확인."""
        settings = BackpressureSettings(metrics_enabled=False)
        exporter = HPAMetricsExporter(settings=settings)

        exporter.start()
        assert not exporter.is_running()

    def test_update_metrics(self):
        """메트릭 업데이트 확인."""
        queue_size = 500
        mock_metrics = MagicMock()
        mock_controller = MagicMock()
        mock_controller.get_state.return_value = RateControllerState(
            current_rate=100.0,
            target_rate=1000.0,
            level=BackpressureLevel.MEDIUM,
            queue_size=queue_size,
            processed_count=1000,
            dropped_count=10,
        )

        exporter = HPAMetricsExporter(
            queue_size_provider=lambda: queue_size,
            rate_controller=mock_controller,
            metrics=mock_metrics,
            component_name="test",
            queue_name="test_queue",
        )

        # 즉시 업데이트
        exporter.update_now()

        # 메트릭 호출 확인
        mock_metrics.set_queue_depth.assert_called_once_with("test_queue", queue_size)
        mock_metrics.set_processing_rate.assert_called_once_with("test", 100.0)
        mock_metrics.set_backpressure_level.assert_called_once_with(
            "test", 2
        )  # MEDIUM = 2

    def test_update_metrics_with_exception(self):
        """메트릭 업데이트 중 예외 처리 확인."""

        def failing_provider():
            raise RuntimeError("Connection error")

        exporter = HPAMetricsExporter(queue_size_provider=failing_provider)

        # 예외가 발생해도 크래시하지 않음
        exporter.update_now()

    def test_double_start(self):
        """중복 시작 방지 확인."""
        settings = BackpressureSettings(hpa_enabled=True, metrics_enabled=True)
        exporter = HPAMetricsExporter(settings=settings)

        exporter.start()
        worker1 = exporter._worker

        exporter.start()
        worker2 = exporter._worker

        # 같은 worker (재시작하지 않음)
        assert worker1 is worker2

        exporter.stop()


class TestHPAMetricsExporterSingleton:
    """HPAMetricsExporter 싱글톤 테스트."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """각 테스트 전 싱글톤 리셋."""
        reset_hpa_metrics_exporter()
        yield
        reset_hpa_metrics_exporter()

    def test_singleton_returns_same_instance(self):
        """싱글톤이 동일 인스턴스 반환 확인."""
        exporter1 = get_hpa_metrics_exporter()
        exporter2 = get_hpa_metrics_exporter()

        assert exporter1 is exporter2

    def test_reset_clears_singleton(self):
        """리셋 후 새 인스턴스 반환 확인."""
        exporter1 = get_hpa_metrics_exporter()
        reset_hpa_metrics_exporter()
        exporter2 = get_hpa_metrics_exporter()

        assert exporter1 is not exporter2
