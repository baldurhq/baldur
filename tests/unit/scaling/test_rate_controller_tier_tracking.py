"""
RateController Tier별 Drop 카운터·CPU 감쇠·동적 Watermark 단위 테스트.

테스트 항목:
- 계약: dropped_by_tier 초기 상태 (3개 tier, 모두 0)
- 동작: watermark/전략 거부 시 해당 tier 카운터만 증가
- 동작: CPU 사용률 기반 Rate 감쇠 배율
- 동작: _adjust_rate()에 리소스 배율 반영
- 동작: should_process()가 settings 필드의 동적 watermark 사용
"""

from unittest.mock import patch

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


class TestDroppedByTierContract:
    """dropped_by_tier 초기 상태 계약 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_initial_state_has_three_tiers(self):
        """초기 dropped_by_tier는 3개 tier를 포함한다."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)
        state = controller.get_state()
        assert set(state.dropped_by_tier.keys()) == {
            "critical",
            "standard",
            "non_essential",
        }

    def test_initial_counts_are_zero(self):
        """초기 tier별 drop 카운터는 모두 0이다."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)
        state = controller.get_state()
        for tier, count in state.dropped_by_tier.items():
            assert count == 0, f"{tier} tier initial count should be 0"


class TestDroppedByTierIncrementBehavior:
    """watermark/전략 거부 시 tier별 카운터 증가 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_watermark_rejection_increments_tier_counter(self):
        """watermark 거부 시 해당 tier 카운터가 1 증가한다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰을 거의 소진하여 non_essential watermark 미만으로 만듦
        for _ in range(9):
            controller._token_bucket.consume()

        before = controller.get_state().dropped_by_tier["non_essential"]
        controller.should_process(priority="non_essential")
        after = controller.get_state().dropped_by_tier["non_essential"]

        assert after == before + 1

    def test_reject_strategy_increments_tier_counter(self):
        """REJECT 전략 거부 시 해당 tier 카운터가 1 증가한다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰 소진 (critical은 watermark=0.0이므로 watermark 통과, 토큰 부족으로 REJECT)
        controller._token_bucket.consume()

        before = controller.get_state().dropped_by_tier["critical"]
        controller.should_process(priority="critical")
        after = controller.get_state().dropped_by_tier["critical"]

        assert after == before + 1

    def test_other_tier_counters_unchanged(self):
        """non_essential 거부 시 critical/standard 카운터는 변하지 않는다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        for _ in range(9):
            controller._token_bucket.consume()

        before_critical = controller.get_state().dropped_by_tier["critical"]
        before_standard = controller.get_state().dropped_by_tier["standard"]

        controller.should_process(priority="non_essential")

        after_critical = controller.get_state().dropped_by_tier["critical"]
        after_standard = controller.get_state().dropped_by_tier["standard"]

        assert after_critical == before_critical
        assert after_standard == before_standard

    def test_total_dropped_also_incremented(self):
        """tier별 카운터 증가 시 전체 dropped_count도 함께 증가한다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        for _ in range(9):
            controller._token_bucket.consume()

        before_total = controller.get_state().dropped_count
        controller.should_process(priority="non_essential")
        after_total = controller.get_state().dropped_count

        assert after_total == before_total + 1


class TestCpuResourceMultiplierBehavior:
    """CPU 사용률 기반 Rate 감쇠 배율 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_normal_cpu_returns_full_rate(self):
        """CPU 사용률이 high 임계치 미만이면 배율 1.0을 반환한다."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)

        with patch(
            "baldur.services.system_metrics_cache.get_cached_cpu_percent",
            return_value=50.0,
        ):
            multiplier = controller._get_resource_pressure_multiplier()

        assert multiplier == 1.0

    def test_high_cpu_returns_half_rate(self):
        """CPU 사용률이 high 임계치 이상이면 배율 0.5를 반환한다."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)

        with patch(
            "baldur.services.system_metrics_cache.get_cached_cpu_percent",
            return_value=settings.resource_cpu_high_threshold,
        ):
            multiplier = controller._get_resource_pressure_multiplier()

        assert multiplier == 0.5

    def test_critical_cpu_returns_tenth_rate(self):
        """CPU 사용률이 critical 임계치 이상이면 배율 0.1을 반환한다."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)

        with patch(
            "baldur.services.system_metrics_cache.get_cached_cpu_percent",
            return_value=settings.resource_cpu_critical_threshold,
        ):
            multiplier = controller._get_resource_pressure_multiplier()

        assert multiplier == 0.1

    def test_exception_returns_full_rate(self):
        """get_cached_cpu_percent() 예외 시 배율 1.0을 반환한다."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)

        with patch(
            "baldur.services.system_metrics_cache.get_cached_cpu_percent",
            side_effect=RuntimeError("unavailable"),
        ):
            multiplier = controller._get_resource_pressure_multiplier()

        assert multiplier == 1.0


class TestAdjustRateResourceMultiplierBehavior:
    """_adjust_rate()에서 CPU 리소스 배율 반영 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_high_cpu_reduces_rate(self):
        """CPU high 상태에서 _adjust_rate() 후 rate가 max보다 낮아진다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1000.0,
            min_rate_per_second=10.0,
        )
        controller = RateController(settings=settings)

        with patch(
            "baldur.services.system_metrics_cache.get_cached_cpu_percent",
            return_value=settings.resource_cpu_high_threshold,
        ):
            controller._adjust_rate()

        state = controller.get_state()
        assert state.current_rate < settings.max_rate_per_second


class TestDynamicWatermarkBehavior:
    """should_process()가 settings 필드의 동적 watermark를 사용하는 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_lowered_watermark_allows_more_traffic(self):
        """watermark를 낮추면 동일 토큰 상태에서도 해당 tier가 허용된다."""
        # 기본 watermark=0.6으로 거부되는 상황
        settings_default = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller_default = RateController(settings=settings_default)

        for _ in range(7):
            controller_default._token_bucket.consume()

        result_default = controller_default.should_process(priority="non_essential")
        assert result_default is False

        # watermark=0.0으로 낮추면 watermark 단계 통과
        settings_low = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            watermark_non_essential=0.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller_low = RateController(settings=settings_low)

        for _ in range(7):
            controller_low._token_bucket.consume()

        result_low = controller_low.should_process(priority="non_essential")
        # watermark=0.0이므로 watermark 통과, 토큰 ~3개 남아 consume 가능
        assert result_low is True
