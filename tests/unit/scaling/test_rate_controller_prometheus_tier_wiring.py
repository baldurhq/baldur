"""
RateController → Prometheus Per-Tier 메트릭 연동 단위 테스트.

테스트 항목:
- 동작: watermark 거부 시 Prometheus dropped_by_tier 카운터 증가
- 동작: 토큰 소진 REJECT 거부 시 Prometheus dropped_by_tier 카운터 증가
- 동작: 허용 시 Prometheus processed_by_tier 카운터 증가
- 동작: metrics=None 시 Prometheus 미발행 (예외 없음)
- 동작: get_rate_controller() 싱글톤이 BackpressureMetrics를 주입
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.scaling.config import (
    BackpressureSettings,
    BackpressureStrategy,
    reset_backpressure_settings,
)
from baldur.scaling.rate_controller import (
    RateController,
    reset_rate_controller,
)


class TestPrometheusDroppedByTierWiringBehavior:
    """RateController 거부 시 Prometheus per-tier dropped 카운터 연동 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_watermark_reject_emits_prometheus_dropped(self):
        """watermark 거부 시 inc_dropped_by_tier()가 호출된다."""
        mock_metrics = MagicMock()
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings, metrics=mock_metrics)

        # 토큰 소진하여 non_essential watermark(0.6) 미만으로 만듦
        for _ in range(9):
            controller._token_bucket.consume()

        controller.should_process(priority="non_essential")

        mock_metrics.inc_dropped_by_tier.assert_called_with("non_essential")

    def test_token_exhaustion_reject_emits_prometheus_dropped(self):
        """REJECT 전략 토큰 부족 거부 시 inc_dropped_by_tier()가 호출된다."""
        mock_metrics = MagicMock()
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings, metrics=mock_metrics)

        # critical은 watermark=0.0이므로 watermark 통과, 토큰 부족으로 REJECT
        controller._token_bucket.consume()

        controller.should_process(priority="critical")

        mock_metrics.inc_dropped_by_tier.assert_called_with("critical")

    def test_throttle_timeout_reject_emits_prometheus_dropped(self):
        """THROTTLE 전략 대기 실패 시 inc_dropped_by_tier()가 호출된다."""
        mock_metrics = MagicMock()
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1.0,
            default_strategy=BackpressureStrategy.THROTTLE,
        )
        controller = RateController(settings=settings, metrics=mock_metrics)

        # 토큰 소진
        controller._token_bucket.consume()

        # THROTTLE 대기 0.1초, rate=1.0이므로 0.1초면 0.1토큰 → 부족
        controller.should_process(priority="standard")

        mock_metrics.inc_dropped_by_tier.assert_called_with("standard")


class TestPrometheusProcessedByTierWiringBehavior:
    """RateController 허용 시 Prometheus per-tier processed 카운터 연동 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_allow_emits_prometheus_processed(self):
        """요청 허용 시 inc_processed_by_tier()가 호출된다."""
        mock_metrics = MagicMock()
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=100.0,
        )
        controller = RateController(settings=settings, metrics=mock_metrics)

        controller.should_process(priority="standard")

        mock_metrics.inc_processed_by_tier.assert_called_with("standard")

    def test_allow_emits_correct_tier(self):
        """각 tier 허용 시 정확한 tier 이름이 Prometheus에 전달된다."""
        mock_metrics = MagicMock()
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=100.0,
        )
        controller = RateController(settings=settings, metrics=mock_metrics)

        controller.should_process(priority="critical")
        mock_metrics.inc_processed_by_tier.assert_called_with("critical")

        mock_metrics.reset_mock()
        controller.should_process(priority="non_essential")
        mock_metrics.inc_processed_by_tier.assert_called_with("non_essential")


class TestNoMetricsEmissionWithoutInstanceBehavior:
    """metrics=None 시 Prometheus 미발행 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_no_error_when_metrics_is_none_on_reject(self):
        """metrics=None 상태에서 거부 시 예외가 발생하지 않는다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings, metrics=None)

        for _ in range(9):
            controller._token_bucket.consume()

        # 예외 없이 거부 처리
        result = controller.should_process(priority="non_essential")
        assert result is False

    def test_no_error_when_metrics_is_none_on_allow(self):
        """metrics=None 상태에서 허용 시 예외가 발생하지 않는다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=100.0,
        )
        controller = RateController(settings=settings, metrics=None)

        # 예외 없이 허용 처리
        result = controller.should_process(priority="standard")
        assert result is True


class TestGetRateControllerSingletonMetricsWiringBehavior:
    """get_rate_controller() 싱글톤이 BackpressureMetrics를 주입하는 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_singleton_has_metrics_instance(self):
        """get_rate_controller()로 생성된 인스턴스에 metrics가 설정되어 있다."""
        from baldur.scaling.rate_controller import get_rate_controller

        mock_metrics = MagicMock()
        with patch(
            "baldur.scaling.metrics.get_backpressure_metrics",
            return_value=mock_metrics,
        ):
            controller = get_rate_controller()

        assert controller._metrics is mock_metrics
