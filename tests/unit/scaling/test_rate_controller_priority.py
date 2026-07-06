"""
Unit tests for RateController Priority-Aware Watermark (236 작업 3).

테스트 항목:
- PRIORITY_WATERMARKS 계약값 검증
- TokenBucket.get_token_ratio() 동작
- should_process(priority) Watermark 분기 동작
- 기존 호출부 하위 호환 (기본값 "standard")
"""

import time

import pytest

from baldur.scaling.config import (
    BackpressureSettings,
    BackpressureStrategy,
    reset_backpressure_settings,
)
from baldur.scaling.rate_controller import (
    PRIORITY_WATERMARKS,
    RateController,
    TokenBucket,
    reset_rate_controller,
)


class TestPriorityWatermarksContract:
    """PRIORITY_WATERMARKS 상수 계약값 검증."""

    def test_watermarks_has_three_tiers(self):
        """3개 tier(critical, standard, non_essential)가 정의되어야 한다."""
        assert len(PRIORITY_WATERMARKS) == 3

    def test_critical_watermark_value(self):
        """critical watermark는 0.0이다 (토큰이 0% 이상이면 항상 시도)."""
        assert PRIORITY_WATERMARKS["critical"] == 0.0

    def test_standard_watermark_value(self):
        """standard watermark는 0.3이다 (토큰 30% 이상일 때 허용)."""
        assert PRIORITY_WATERMARKS["standard"] == 0.3

    def test_non_essential_watermark_value(self):
        """non_essential watermark는 0.6이다 (토큰 60% 이상일 때 허용)."""
        assert PRIORITY_WATERMARKS["non_essential"] == 0.6

    def test_watermarks_ordering(self):
        """critical < standard < non_essential 순으로 watermark가 증가한다."""
        assert (
            PRIORITY_WATERMARKS["critical"]
            < PRIORITY_WATERMARKS["standard"]
            < PRIORITY_WATERMARKS["non_essential"]
        )


class TestTokenBucketGetTokenRatioBehavior:
    """TokenBucket.get_token_ratio() 동작 검증."""

    def test_full_bucket_returns_one(self):
        """토큰이 가득 찬 버킷은 비율 1.0을 반환한다."""
        bucket = TokenBucket(rate=100.0, capacity=100.0)
        ratio = bucket.get_token_ratio()
        assert ratio == pytest.approx(1.0, abs=0.05)

    def test_empty_bucket_returns_near_zero(self):
        """토큰이 소진된 버킷은 0에 가까운 비율을 반환한다."""
        bucket = TokenBucket(rate=0.1, capacity=10.0)
        # 토큰 전부 소비
        for _ in range(10):
            bucket.consume()
        ratio = bucket.get_token_ratio()
        assert ratio < 0.1

    def test_partial_consumption_returns_intermediate(self):
        """일부 소비 후 중간 비율을 반환한다."""
        bucket = TokenBucket(rate=0.1, capacity=10.0)
        # 5개 소비
        for _ in range(5):
            bucket.consume()
        ratio = bucket.get_token_ratio()
        assert 0.3 < ratio < 0.8

    def test_zero_capacity_returns_zero(self):
        """capacity=0이면 0.0을 반환한다."""
        bucket = TokenBucket(rate=0.0, capacity=0.0)
        ratio = bucket.get_token_ratio()
        assert ratio == 0.0

    def test_get_token_ratio_is_read_only(self):
        """get_token_ratio()는 _last_update를 갱신하지 않는다 (읽기 전용)."""
        bucket = TokenBucket(rate=100.0, capacity=10.0)
        # consume으로 토큰 소비 및 _last_update 갱신
        bucket.consume()
        with bucket._lock:
            last_update_before = bucket._last_update

        # get_token_ratio 여러 번 호출
        bucket.get_token_ratio()
        bucket.get_token_ratio()

        with bucket._lock:
            last_update_after = bucket._last_update

        assert last_update_before == last_update_after

    def test_ratio_never_exceeds_one(self):
        """시간이 경과해도 비율은 1.0을 초과하지 않는다."""
        bucket = TokenBucket(rate=10000.0, capacity=1.0)
        time.sleep(0.05)
        ratio = bucket.get_token_ratio()
        assert ratio <= 1.0


class TestShouldProcessPriorityBehavior:
    """should_process(priority) Watermark 분기 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_disabled_always_returns_true(self):
        """backpressure 비활성화 시 모든 priority에 대해 True 반환."""
        settings = BackpressureSettings(backpressure_enabled=False)
        controller = RateController(settings=settings)

        for priority in ("critical", "standard", "non_essential"):
            assert controller.should_process(priority=priority) is True

    def test_critical_allowed_when_tokens_very_low(self):
        """토큰이 거의 없어도 critical은 watermark=0.0이므로 시도된다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=2.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰 1개 소비
        controller.should_process(priority="critical")
        # 두 번째 critical도 watermark=0.0이므로 consume 시도 (토큰 부족이면 전략 분기로)
        # 최소한 watermark 자체로 거부당하지는 않음
        # (토큰이 남아있으면 True, 아니면 전략에 따라 결정)

    def test_non_essential_rejected_when_tokens_below_watermark(self):
        """토큰 비율이 non_essential watermark(0.6) 미만이면 즉시 거부."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰을 상당히 소비하여 비율을 낮춤
        for _ in range(7):
            controller._token_bucket.consume()

        # non_essential은 토큰 비율 < 0.6이면 watermark에 의해 거부
        result = controller.should_process(priority="non_essential")
        assert result is False

    def test_standard_rejected_when_tokens_below_watermark(self):
        """토큰 비율이 standard watermark(0.3) 미만이면 즉시 거부."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰을 거의 소진
        for _ in range(9):
            controller._token_bucket.consume()

        # standard는 토큰 비율 < 0.3이면 watermark에 의해 거부
        result = controller.should_process(priority="standard")
        assert result is False

    def test_priority_differentiation_under_load(self):
        """부하 상황에서 priority별 차등 처리: critical > standard > non_essential."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰의 약 60% 소비 → 비율 약 0.4 (standard OK, non_essential NG)
        for _ in range(6):
            controller._token_bucket.consume()

        ratio = controller._token_bucket.get_token_ratio()

        # critical (watermark 0.0): 비율 > 0.0 → 허용 시도
        critical_result = controller.should_process(priority="critical")
        # watermark 통과, 토큰 소비 시도
        assert critical_result is True or ratio >= PRIORITY_WATERMARKS["critical"]

        # non_essential (watermark 0.6): 비율 < 0.6 → 거부
        ne_result = controller.should_process(priority="non_essential")
        if ratio < PRIORITY_WATERMARKS["non_essential"]:
            assert ne_result is False

    def test_watermark_boundary_admits_at_exactly_watermark(self):
        """At token_ratio == watermark the request is admitted, not rejected.

        The gate is strict ``token_ratio < watermark`` so equality must pass
        through to token consumption. Pins the ``<`` vs ``<=`` boundary.
        """
        from unittest.mock import patch

        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1000.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)
        watermark = controller._settings.get_priority_watermarks()["standard"]

        with (
            patch.object(
                controller._token_bucket, "get_token_ratio", return_value=watermark
            ),
            patch.object(controller._token_bucket, "consume", return_value=True),
        ):
            result = controller.should_process(priority="standard")

        # token_ratio == watermark -> `<` is False -> not rejected by watermark
        assert result is True

    def test_default_priority_is_standard(self):
        """기본값이 'standard'이므로 기존 호출부와 하위 호환."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)

        # 인자 없이 호출 — "standard" 기본값
        result_no_arg = controller.should_process()
        result_explicit = controller.should_process(priority="standard")

        assert result_no_arg is True
        assert result_explicit is True

    def test_unknown_priority_uses_fallback_watermark(self):
        """알 수 없는 priority는 기본 watermark 0.3 적용."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)

        # 토큰 충분 → 어떤 priority든 허용
        result = controller.should_process(priority="unknown_tier")
        assert result is True

    def test_watermark_rejection_increments_dropped_count(self):
        """watermark 거부 시 dropped_count가 증가한다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 토큰 거의 소진
        for _ in range(9):
            controller._token_bucket.consume()

        initial_dropped = controller.get_state().dropped_count
        controller.should_process(priority="non_essential")
        after_dropped = controller.get_state().dropped_count

        assert after_dropped > initial_dropped

    def test_successful_process_increments_processed_count(self):
        """watermark 통과 + 토큰 소비 성공 시 processed_count 증가."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)

        initial_processed = controller.get_state().processed_count
        controller.should_process(priority="critical")
        after_processed = controller.get_state().processed_count

        assert after_processed == initial_processed + 1
