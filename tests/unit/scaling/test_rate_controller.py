"""
Unit tests for TokenBucket and RateController.

테스트 항목:
- TokenBucket 토큰 소비
- TokenBucket Rate 변경
- RateController 상태 관리
- RateController should_process 전략별 동작
- RateController AIMD Rate 조절
"""

import time
from unittest.mock import patch

import pytest

from baldur.scaling.config import (
    BackpressureLevel,
    BackpressureSettings,
    BackpressureStrategy,
    reset_backpressure_settings,
)
from baldur.scaling.rate_controller import (
    RateController,
    RateControllerState,
    TokenBucket,
    get_rate_controller,
    reset_rate_controller,
)


class TestTokenBucket:
    """TokenBucket 테스트."""

    def test_initial_tokens_available(self):
        """초기화 시 토큰이 사용 가능한지 확인."""
        bucket = TokenBucket(rate=10.0)
        assert bucket.consume() is True

    def test_consume_reduces_tokens(self):
        """토큰 소비 시 토큰이 감소하는지 확인."""
        bucket = TokenBucket(rate=1.0, capacity=2.0)
        assert bucket.consume() is True
        assert bucket.consume() is True
        # 토큰 소진
        assert bucket.consume() is False

    def test_tokens_refill_over_time(self):
        """시간이 지나면 토큰이 충전되는지 확인."""
        bucket = TokenBucket(rate=100.0, capacity=10.0)

        # 토큰 소진
        for _ in range(10):
            bucket.consume()

        # 토큰 부족
        assert bucket.consume() is False

        # 시간 경과 후 토큰 충전
        time.sleep(0.05)  # 5 tokens should be added
        assert bucket.consume() is True

    def test_set_rate(self):
        """Rate 변경 테스트."""
        bucket = TokenBucket(rate=10.0)
        bucket.set_rate(100.0)
        assert bucket.get_rate() == 100.0

    def test_wait_for_token_success(self):
        """토큰 대기 성공 테스트."""
        bucket = TokenBucket(rate=100.0, capacity=1.0)
        bucket.consume()  # 토큰 소진

        # 대기 후 토큰 획득
        assert bucket.wait_for_token(timeout=0.1) is True

    def test_wait_for_token_timeout(self):
        """토큰 대기 타임아웃 테스트."""
        bucket = TokenBucket(rate=0.1, capacity=1.0)  # 매우 느린 rate
        bucket.consume()  # 토큰 소진

        # 타임아웃
        assert bucket.wait_for_token(timeout=0.01) is False


class TestRateControllerState:
    """RateControllerState 테스트."""

    def test_state_dataclass(self):
        """상태 데이터클래스 생성 테스트."""
        state = RateControllerState(
            current_rate=500.0,
            target_rate=1000.0,
            level=BackpressureLevel.MEDIUM,
            queue_size=600,
            processed_count=100,
            dropped_count=10,
        )

        assert state.current_rate == 500.0
        assert state.target_rate == 1000.0
        assert state.level == BackpressureLevel.MEDIUM
        assert state.queue_size == 600
        assert state.processed_count == 100
        assert state.dropped_count == 10


class TestRateController:
    """RateController 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_initial_state(self):
        """초기 상태 확인."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)

        state = controller.get_state()
        assert state.current_rate == settings.max_rate_per_second
        assert state.level == BackpressureLevel.NONE
        assert state.processed_count == 0
        assert state.dropped_count == 0

    def test_should_process_when_enabled(self):
        """활성화 시 should_process 동작."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)

        # 높은 rate이므로 처리 가능
        assert controller.should_process() is True

    def test_should_process_when_disabled(self):
        """비활성화 시 should_process는 항상 True."""
        settings = BackpressureSettings(backpressure_enabled=False)
        controller = RateController(settings=settings)

        # 비활성화 시 항상 True
        for _ in range(100):
            assert controller.should_process() is True

    def test_should_process_increments_processed_count(self):
        """should_process 성공 시 processed_count 증가."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)

        controller.should_process()
        controller.should_process()
        controller.should_process()

        state = controller.get_state()
        assert state.processed_count == 3

    def test_reject_strategy_drops_on_rate_limit(self):
        """REJECT 전략에서 rate limit 초과 시 드롭."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1.0,  # 매우 낮은 rate
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)

        # 첫 번째는 성공
        assert controller.should_process() is True

        # 두 번째는 드롭 (토큰 부족)
        assert controller.should_process() is False

        state = controller.get_state()
        assert state.dropped_count >= 1

    def test_queue_size_provider(self):
        """큐 크기 제공자 테스트."""
        queue_size = 500

        settings = BackpressureSettings()
        controller = RateController(
            settings=settings,
            queue_size_provider=lambda: queue_size,
        )

        state = controller.get_state()
        assert state.queue_size == 500

    def test_start_and_stop(self):
        """start/stop 메서드 테스트."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            rate_adjust_interval_seconds=1.0,  # 최소값 1.0
        )
        controller = RateController(settings=settings)

        controller.start()
        time.sleep(0.05)
        controller.stop()

        # 정상 종료 확인
        assert controller._running is False

    def test_start_when_disabled(self):
        """비활성화 시 start 무시."""
        settings = BackpressureSettings(backpressure_enabled=False)
        controller = RateController(settings=settings)

        controller.start()

        # 스레드가 시작되지 않음
        assert controller._worker is None


class TestShouldProcessStrategyDispatch:
    """should_process() backpressure-strategy dispatch after token exhaustion.

    Each test forces the token-exhausted path (``consume()`` returns False) and
    uses ``priority="critical"`` (watermark 0.0) so the watermark gate never
    short-circuits before the strategy dispatch is reached. The default strategy
    is THROTTLE, so without these the production default admit/drop path is
    executed but never asserted.
    """

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def _controller(self, strategy):
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1000.0,
            default_strategy=strategy,
        )
        return RateController(settings=settings)

    def test_throttle_admits_and_records_when_wait_succeeds(self):
        """THROTTLE (default): token exhausted but one frees within the wait
        window -> admit and count as processed."""
        controller = self._controller(BackpressureStrategy.THROTTLE)
        with (
            patch.object(controller._token_bucket, "consume", return_value=False),
            patch.object(controller._token_bucket, "wait_for_token", return_value=True),
        ):
            result = controller.should_process(priority="critical")

        assert result is True
        assert controller.get_state().processed_by_tier["critical"] == 1
        assert controller.get_state().dropped_by_tier["critical"] == 0

    def test_throttle_drops_and_records_when_wait_times_out(self):
        """THROTTLE: token exhausted and none frees within the wait window ->
        reject and count as dropped."""
        controller = self._controller(BackpressureStrategy.THROTTLE)
        with (
            patch.object(controller._token_bucket, "consume", return_value=False),
            patch.object(
                controller._token_bucket, "wait_for_token", return_value=False
            ),
        ):
            result = controller.should_process(priority="critical")

        assert result is False
        assert controller.get_state().dropped_by_tier["critical"] == 1
        assert controller.get_state().processed_by_tier["critical"] == 0

    def test_drop_oldest_admits_without_recording(self):
        """DROP_OLDEST: caller handles the drop, so should_process admits (True)
        without touching the processed/dropped counters."""
        controller = self._controller(BackpressureStrategy.DROP_OLDEST)
        with patch.object(controller._token_bucket, "consume", return_value=False):
            result = controller.should_process(priority="critical")

        assert result is True
        assert controller.get_state().processed_by_tier["critical"] == 0
        assert controller.get_state().dropped_by_tier["critical"] == 0

    def test_queue_admits_without_recording(self):
        """QUEUE: caller enqueues, so should_process admits (True) without
        touching the processed/dropped counters."""
        controller = self._controller(BackpressureStrategy.QUEUE)
        with patch.object(controller._token_bucket, "consume", return_value=False):
            result = controller.should_process(priority="critical")

        assert result is True
        assert controller.get_state().processed_by_tier["critical"] == 0
        assert controller.get_state().dropped_by_tier["critical"] == 0


class TestRateControllerAIMD:
    """RateController AIMD 패턴 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_rate_adjustment_on_high_queue(self):
        """높은 큐 크기에서 Rate 감소."""
        queue_size = 1500  # HIGH 레벨

        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1000.0,
            queue_high_threshold=1000,
        )
        controller = RateController(
            settings=settings,
            queue_size_provider=lambda: queue_size,
        )

        # CPU 부하로 인한 resource_multiplier 간섭 제거
        with patch.object(
            controller, "_get_resource_pressure_multiplier", return_value=1.0
        ):
            controller._adjust_rate()

        state = controller.get_state()
        assert state.level == BackpressureLevel.HIGH
        # HIGH 레벨: 80% 배율 적용
        expected_rate = 1000.0 * 0.8
        assert state.current_rate == expected_rate
        # The computed rate must propagate to the token bucket (the actual gate
        # that admits/rejects traffic), not just the mirror field.
        assert controller._token_bucket.get_rate() == expected_rate

    def test_rate_adjustment_on_critical_queue(self):
        """CRITICAL 큐 크기에서 Rate 급감."""
        queue_size = 6000  # CRITICAL 레벨

        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1000.0,
            queue_critical_threshold=5000,
        )
        controller = RateController(
            settings=settings,
            queue_size_provider=lambda: queue_size,
        )

        with patch.object(
            controller, "_get_resource_pressure_multiplier", return_value=1.0
        ):
            controller._adjust_rate()

        state = controller.get_state()
        assert state.level == BackpressureLevel.CRITICAL
        # CRITICAL 레벨: 50% 배율 적용
        expected_rate = 1000.0 * 0.5
        assert state.current_rate == expected_rate

    def test_rate_increase_on_normal_queue(self):
        """정상 큐 크기에서 Rate 점진적 증가."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1000.0,
            min_rate_per_second=100.0,
            rate_increase_factor=1.1,
        )
        controller = RateController(
            settings=settings,
            queue_size_provider=lambda: 0,  # NONE 레벨
        )

        # 초기 Rate을 낮춤
        controller._current_rate = 500.0
        controller._token_bucket.set_rate(500.0)

        with patch.object(
            controller, "_get_resource_pressure_multiplier", return_value=1.0
        ):
            controller._adjust_rate()

        state = controller.get_state()
        assert state.level == BackpressureLevel.NONE
        # 1.1배 증가 (최대값 제한)
        expected_rate = min(500.0 * 1.1, 1000.0)
        assert state.current_rate == expected_rate


class TestRateControllerSingleton:
    """RateController 싱글톤 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    @patch(
        "baldur.scaling.metrics.get_backpressure_metrics",
        return_value=None,
    )
    def test_singleton_returns_same_instance(self, _mock_metrics):
        """싱글톤이 동일한 인스턴스를 반환하는지 확인."""
        controller1 = get_rate_controller()
        controller2 = get_rate_controller()
        assert controller1 is controller2

    @patch(
        "baldur.scaling.metrics.get_backpressure_metrics",
        return_value=None,
    )
    def test_reset_clears_singleton(self, _mock_metrics):
        """리셋 후 새 인스턴스가 생성되는지 확인."""
        controller1 = get_rate_controller()
        reset_rate_controller()
        controller2 = get_rate_controller()
        assert controller1 is not controller2
