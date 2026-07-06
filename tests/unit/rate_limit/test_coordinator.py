"""
RateLimitCoordinator 단위 테스트.

테스트 대상:
- 429 이벤트 발행
- 지수 백오프
- 디바운싱 윈도우
- Canary Request 모드
- Cooldown 상태
- retry_after 헤더 우선 사용
- Fail-Open 동작
- 메트릭 기록
- rate_limit_aware 데코레이터
- on_success, _schedule_cooldown_end 스케줄링
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time

from tests.unit.rate_limit.conftest import (
    DEFAULT_BACKOFF_MULTIPLIER,
    DEFAULT_BASE_DELAY,
    DEFAULT_DEBOUNCE_WINDOW,
    DEFAULT_MAX_DELAY,
    DEFAULT_RETRY_AFTER,
    MockInMemoryRateLimitStorage,
    make_mock_event_bus,
)

# =============================================================================
# 이벤트 발행 테스트
# =============================================================================


class TestRateLimitCoordinatorEventEmission:
    """RateLimitCoordinator 이벤트 발행 테스트."""

    def test_on_rate_limited_emits_429_event(self, mock_storage):
        """on_rate_limited() 호출 시 RATE_LIMIT_429 이벤트 발행."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            base_delay=DEFAULT_RETRY_AFTER,
            debounce_window_seconds=DEFAULT_DEBOUNCE_WINDOW,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        mock_bus, emitted_events = make_mock_event_bus()

        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_get_bus.return_value = mock_bus
            coordinator.on_rate_limited("payment_api", retry_after=5)

        rate_limit_events = [
            e for e in emitted_events if "RATE_LIMIT_429" in e["event_type"]
        ]
        assert len(rate_limit_events) >= 1

        event_data = rate_limit_events[0]["data"]
        assert event_data["key"] == "payment_api"
        assert event_data["consecutive_429s"] == 1

    @pytest.mark.parametrize(
        ("call_index", "expected_multiplier"),
        [
            (0, 1),  # 2^0 = 1
            (1, 2),  # 2^1 = 2
            (2, 4),  # 2^2 = 4
        ],
        ids=["first-429", "second-429", "third-429"],
    )
    def test_on_rate_limited_calculates_exponential_backoff(
        self, mock_storage, call_index, expected_multiplier
    ):
        """연속 429 시 지수 백오프 계산 확인."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        base = DEFAULT_BASE_DELAY
        config = RateLimitCoordinatorConfig(
            base_delay=base,
            default_retry_after=base,
            backoff_multiplier=DEFAULT_BACKOFF_MULTIPLIER,
            max_delay=DEFAULT_MAX_DELAY,
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        delay = None
        for _ in range(call_index + 1):
            delay = coordinator.on_rate_limited("test_api")

        expected = base * expected_multiplier
        assert delay == pytest.approx(expected, rel=0.1)


# =============================================================================
# 디바운싱 테스트
# =============================================================================


class TestRateLimitCoordinatorDebouncing:
    """RateLimitCoordinator 디바운싱 테스트."""

    @freeze_time("2026-02-06 12:00:00")
    def test_debounce_window_prevents_duplicate_events(self, mock_storage):
        """디바운싱 윈도우 내 중복 이벤트 방지."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            debounce_window_seconds=DEFAULT_DEBOUNCE_WINDOW
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        assert coordinator._should_emit_event("test_api") is True
        assert coordinator._should_emit_event("test_api") is False

    @freeze_time("2026-02-06 12:00:00")
    def test_debounce_window_expires_after_timeout(self, mock_storage):
        """디바운싱 윈도우 만료 후 이벤트 발행 허용."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        window = DEFAULT_DEBOUNCE_WINDOW
        config = RateLimitCoordinatorConfig(debounce_window_seconds=window)
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        assert coordinator._should_emit_event("test_api") is True

        expired_time = f"2026-02-06 12:00:{int(window) + 1:02d}"
        with freeze_time(expired_time):
            assert coordinator._should_emit_event("test_api") is True

    @freeze_time("2026-02-06 12:00:00")
    def test_debounce_tracks_keys_independently(self, mock_storage):
        """서로 다른 key는 독립적으로 디바운싱."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            debounce_window_seconds=DEFAULT_DEBOUNCE_WINDOW
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        assert coordinator._should_emit_event("api_a") is True
        assert coordinator._should_emit_event("api_b") is True
        assert coordinator._should_emit_event("api_a") is False

    def test_debounce_skips_event_and_metrics(self, mock_storage):
        """디바운싱 윈도우 내에서 이벤트와 메트릭이 스킵됨."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            debounce_window_seconds=10.0,
            jitter_percent=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        emit_count = 0

        def count_emit(event_type, data, source, priority):
            nonlocal emit_count
            emit_count += 1
            return 1

        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_bus.emit = count_emit
            mock_get_bus.return_value = mock_bus

            coordinator.on_rate_limited("test_api")
            first_count = emit_count
            coordinator.on_rate_limited("test_api")

        assert emit_count == first_count


# =============================================================================
# Canary Request 테스트
# =============================================================================


class TestRateLimitCoordinatorCanary:
    """RateLimitCoordinator Canary Request 테스트."""

    def test_wait_if_needed_returns_canary_after_429(self, mock_storage):
        """429 발생 후 첫 요청은 Canary 모드."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        mock_storage.increment_consecutive_429s("test_api")
        result = coordinator.wait_if_needed("test_api")
        assert result.is_canary is True

    def test_on_success_clears_canary_state(self, mock_storage):
        """성공 후 Canary 상태 해제."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        mock_storage.increment_consecutive_429s("test_api")
        result1 = coordinator.wait_if_needed("test_api")
        assert result1.is_canary is True

        coordinator.on_success("test_api")

        result2 = coordinator.wait_if_needed("test_api")
        assert result2.is_canary is False


# =============================================================================
# Cooldown 상태 테스트
# =============================================================================


class TestRateLimitCoordinatorCooldown:
    """RateLimitCoordinator Cooldown 테스트."""

    def test_cooldown_state_detection(self):
        """Cooldown 상태 감지 테스트."""
        storage = MockInMemoryRateLimitStorage()

        cooldown_duration = 10.0
        cooldown_until = time.time() + cooldown_duration
        storage.set_cooldown("test_api", cooldown_until)
        storage.increment_consecutive_429s("test_api")

        state = storage.get_state("test_api")
        assert state.is_in_cooldown is True
        assert 0 < state.remaining_cooldown <= cooldown_duration

    def test_cooldown_expired(self):
        """Cooldown 만료 테스트."""
        storage = MockInMemoryRateLimitStorage()

        storage.set_cooldown("test_api", time.time() - 5.0)

        state = storage.get_state("test_api")
        assert state.is_in_cooldown is False
        assert state.remaining_cooldown == 0.0


# =============================================================================
# Fail-Open 동작 테스트
# =============================================================================


class TestEmitRateLimitEventFailOpen:
    """_emit_rate_limit_event Fail-Open 동작 테스트."""

    def test_emit_survives_import_error(self):
        """EventBus import 실패 시 예외 없이 통과 (Fail-Open)."""
        from baldur.services.rate_limit_coordinator import _emit_rate_limit_event

        with patch(
            "baldur.services.rate_limit_coordinator._emit_rate_limit_event",
            wraps=_emit_rate_limit_event,
        ):
            with patch(
                "baldur.services.event_bus.get_event_bus",
                side_effect=ImportError("no module"),
            ):
                _emit_rate_limit_event("RATE_LIMIT_429", {"key": "test"})

    def test_emit_survives_generic_exception(self):
        """EventBus 발행 중 예외 시 Fail-Open."""
        from baldur.services.rate_limit_coordinator import _emit_rate_limit_event

        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=RuntimeError("bus broken"),
        ):
            _emit_rate_limit_event("RATE_LIMIT_429", {"key": "test"})

    def test_emit_unknown_event_type_does_not_crash(self):
        """존재하지 않는 EventType 지정 시 warning 후 통과."""
        from baldur.services.rate_limit_coordinator import _emit_rate_limit_event

        mock_bus = MagicMock()
        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            _emit_rate_limit_event("NONEXISTENT_EVENT_TYPE", {"key": "test"})

        mock_bus.emit.assert_not_called()


# =============================================================================
# 메트릭 기록 테스트
# =============================================================================


class TestRecordRateLimitMetrics:
    """_record_rate_limit_metrics 메트릭 기록 테스트."""

    def test_records_429_counter(self):
        """rate_limit_429_total 카운터 증가 확인."""
        from baldur.services.rate_limit_coordinator import (
            _record_rate_limit_metrics,
        )

        mock_counter = MagicMock()
        mock_labels = MagicMock()
        mock_counter.labels.return_value = mock_labels

        with patch(
            "baldur.services.metrics.definitions.rate_limit_429_total",
            mock_counter,
        ):
            _record_rate_limit_metrics(key="payment_api", status_code=429)

        mock_counter.labels.assert_called_with(key="payment_api", status_code="429")
        mock_labels.inc.assert_called_once()

    def test_records_cooldown_histogram(self):
        """rate_limit_cooldown_seconds 히스토그램 기록 확인."""
        from baldur.services.rate_limit_coordinator import (
            _record_rate_limit_metrics,
        )

        mock_counter = MagicMock()
        mock_counter.labels.return_value = MagicMock()
        mock_histogram = MagicMock()
        mock_hist_labels = MagicMock()
        mock_histogram.labels.return_value = mock_hist_labels

        cooldown_value = 15.5
        with patch(
            "baldur.services.metrics.definitions.rate_limit_429_total",
            mock_counter,
        ):
            with patch(
                "baldur.services.metrics.definitions.rate_limit_cooldown_seconds",
                mock_histogram,
            ):
                _record_rate_limit_metrics(key="test", cooldown_seconds=cooldown_value)

        mock_histogram.labels.assert_called_with(key="test")
        mock_hist_labels.observe.assert_called_with(cooldown_value)

    def test_records_consecutive_gauge(self):
        """rate_limit_consecutive_429s 게이지 설정 확인."""
        from baldur.services.rate_limit_coordinator import (
            _record_rate_limit_metrics,
        )

        mock_counter = MagicMock()
        mock_counter.labels.return_value = MagicMock()
        mock_gauge = MagicMock()
        mock_gauge_labels = MagicMock()
        mock_gauge.labels.return_value = mock_gauge_labels

        consecutive = 5
        with patch(
            "baldur.services.metrics.definitions.rate_limit_429_total",
            mock_counter,
        ):
            with patch(
                "baldur.services.metrics.definitions.rate_limit_consecutive_429s",
                mock_gauge,
            ):
                _record_rate_limit_metrics(key="test", consecutive_429s=consecutive)

        mock_gauge.labels.assert_called_with(key="test")
        mock_gauge_labels.set.assert_called_with(consecutive)

    def test_metrics_fail_open_on_import_error(self):
        """메트릭 모듈 import 실패 시 예외 없이 통과."""
        from baldur.services.rate_limit_coordinator import (
            _record_rate_limit_metrics,
        )

        with patch(
            "baldur.services.metrics.definitions.rate_limit_429_total",
            side_effect=AttributeError("no such metric"),
        ):
            _record_rate_limit_metrics(key="test")


# =============================================================================
# retry_after 헤더 우선 사용 테스트
# =============================================================================


class TestRateLimitCoordinatorRetryAfter:
    """on_rate_limited retry_after 헤더 우선 사용 테스트."""

    def test_uses_retry_after_header_when_provided(self, mock_storage):
        """retry_after 값이 주어지면 default_retry_after 대신 사용."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        default_ra = DEFAULT_RETRY_AFTER
        header_ra = 30.0
        config = RateLimitCoordinatorConfig(
            default_retry_after=default_ra,
            backoff_multiplier=1.0,
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        delay = coordinator.on_rate_limited("test_api", retry_after=header_ra)
        assert delay == pytest.approx(header_ra, rel=0.1)

    def test_uses_default_retry_after_when_none(self, mock_storage):
        """retry_after가 None이면 default_retry_after 사용."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        default_ra = 7.0
        config = RateLimitCoordinatorConfig(
            default_retry_after=default_ra,
            backoff_multiplier=1.0,
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        delay = coordinator.on_rate_limited("test_api", retry_after=None)
        assert delay == pytest.approx(default_ra, rel=0.1)

    def test_max_delay_cap(self, mock_storage):
        """max_delay 상한 캡핑 확인."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        max_delay = 30.0
        config = RateLimitCoordinatorConfig(
            default_retry_after=10.0,
            backoff_multiplier=DEFAULT_BACKOFF_MULTIPLIER,
            max_delay=max_delay,
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        delay = None
        for _ in range(10):
            delay = coordinator.on_rate_limited("test_api")

        assert delay <= max_delay


# =============================================================================
# on_success 동작 테스트
# =============================================================================


class TestRateLimitCoordinatorOnSuccess:
    """on_success() 동작 테스트."""

    def test_on_success_resets_consecutive_429s(self, mock_storage):
        """성공 응답 시 consecutive_429s 리셋."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        mock_storage.increment_consecutive_429s("test_api")
        mock_storage.increment_consecutive_429s("test_api")
        assert mock_storage.get_state("test_api").consecutive_429s == 2

        coordinator.on_success("test_api")
        assert mock_storage.get_state("test_api").consecutive_429s == 0

    def test_on_success_no_error_when_no_prior_429(self, mock_storage):
        """429 없이 on_success 호출 시 에러 없음."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        coordinator.on_success("test_api")
        assert mock_storage.get_state("test_api").consecutive_429s == 0


# =============================================================================
# _schedule_cooldown_end_event 스케줄링 테스트
# =============================================================================


class TestRateLimitCoordinatorScheduleCooldownEnd:
    """_schedule_cooldown_end_event 스케줄링 테스트."""

    def test_schedule_skipped_when_delay_is_zero_or_negative(self, mock_storage):
        """cooldown_until이 과거면 타이머 스케줄링 스킵."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        coordinator._schedule_cooldown_end_event("test_api", time.time() - 5)
        assert "test_api" not in coordinator._cooldown_timers

    def test_schedule_cancels_existing_timer(self, mock_storage):
        """동일 key에 대한 기존 타이머 취소."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        coordinator._schedule_cooldown_end_event("test_api", time.time() + 60)
        first_timer = coordinator._cooldown_timers.get("test_api")
        assert first_timer is not None

        coordinator._schedule_cooldown_end_event("test_api", time.time() + 120)
        second_timer = coordinator._cooldown_timers.get("test_api")
        assert second_timer is not first_timer

        second_timer.cancel()


# =============================================================================
# rate_limit_aware 데코레이터 테스트
# =============================================================================


class TestRateLimitAwareDecorator:
    """rate_limit_aware() 데코레이터 테스트."""

    def test_decorator_calls_wait_and_on_success(self, mock_storage):
        """데코레이터가 wait_if_needed + on_success 호출."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        @coordinator.rate_limit_aware("test_api")
        def call_api():
            return mock_response

        result = call_api()
        assert result.status_code == 200

    def test_decorator_calls_on_rate_limited_on_429(
        self, coordinator_no_jitter_no_debounce, mock_storage
    ):
        """데코레이터가 429 응답 시 on_rate_limited 호출."""
        coordinator = coordinator_no_jitter_no_debounce

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "10"}

        @coordinator.rate_limit_aware("test_api")
        def call_api():
            return mock_response

        call_api()

        state = mock_storage.get_state("test_api")
        assert state.consecutive_429s == 1


# =============================================================================
# 317: _broadcast_to_cluster Kafka 분산 전파 테스트
# =============================================================================


class TestBroadcastToClusterBehavior:
    """317: _broadcast_to_cluster Fail-Open 동작 검증."""

    def test_broadcast_calls_distributed_channel(self, mock_storage):
        """_broadcast_to_cluster가 DistributedRateLimitChannel.broadcast_rate_limit_429 호출."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        mock_channel = MagicMock()
        with patch(
            "baldur.services.rate_limit.distributed_channel.get_distributed_rate_limit_channel",
            return_value=mock_channel,
        ):
            coordinator._broadcast_to_cluster(
                key="payment_api",
                consecutive_429s=3,
                cooldown_until=1000.0,
                calculated_delay=5.0,
            )

        mock_channel.broadcast_rate_limit_429.assert_called_once_with(
            key="payment_api",
            consecutive_429s=3,
            cooldown_until=1000.0,
            calculated_delay=5.0,
        )

    def test_broadcast_fail_open_on_import_error(self, mock_storage):
        """분산 채널 import 실패 시 예외 없이 통과 (Fail-Open)."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        with patch(
            "baldur.services.rate_limit.distributed_channel.get_distributed_rate_limit_channel",
            side_effect=ImportError("no kafka"),
        ):
            coordinator._broadcast_to_cluster(
                key="test",
                consecutive_429s=1,
                cooldown_until=1000.0,
                calculated_delay=5.0,
            )

    def test_broadcast_fail_open_on_runtime_error(self, mock_storage):
        """분산 채널 런타임 에러 시 예외 없이 통과 (Fail-Open)."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        coordinator = RateLimitCoordinator(
            storage=mock_storage, config=RateLimitCoordinatorConfig()
        )

        with patch(
            "baldur.services.rate_limit.distributed_channel.get_distributed_rate_limit_channel",
            side_effect=RuntimeError("channel broken"),
        ):
            coordinator._broadcast_to_cluster(
                key="test",
                consecutive_429s=1,
                cooldown_until=1000.0,
                calculated_delay=5.0,
            )

    def test_on_rate_limited_invokes_broadcast(self, mock_storage):
        """on_rate_limited가 _broadcast_to_cluster를 호출하는지 검증."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        config = RateLimitCoordinatorConfig(
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
        )
        coordinator = RateLimitCoordinator(storage=mock_storage, config=config)

        with patch.object(coordinator, "_broadcast_to_cluster") as mock_broadcast:
            coordinator.on_rate_limited("test_api", retry_after=5.0)

        mock_broadcast.assert_called_once()
        assert mock_broadcast.call_args[0][0] == "test_api"
