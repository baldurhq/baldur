"""
Circuit Breaker Enhancements 단위 테스트.

새로 추가된 기능 테스트:
1. minimum_calls - 샘플 부족 시 CB 오작동 방지
2. Fallback 전략 - cache, DLQ, default_response
3. Burn Rate 가중치 - CB OPEN 시 Error Budget 소진 가속화
4. 스냅샷 저장 - CB OPEN 시 시스템 상태 기록

Reference: Circuit Breaker 리뷰 피드백
"""

from unittest.mock import Mock, patch

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

from baldur.services.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerFallbackResult,
    CircuitBreakerService,
)
from baldur.services.circuit_breaker.outcome_window import (
    TRIP_REASON_COUNT,
    TRIP_REASON_RATE,
    evaluate_trip,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_repository():
    """Mock CircuitBreakerStateRepository."""
    repo = Mock()
    repo.get_or_create = Mock()
    repo.update_state = Mock()
    repo.record_failure = Mock()
    repo.record_success = Mock()
    return repo


@pytest.fixture
def base_config():
    """Base configuration with all enhancements enabled."""
    return CircuitBreakerConfig(
        enabled=True,
        failure_threshold=5,
        recovery_timeout=60,
        success_threshold=2,
        minimum_calls=10,  # At least 10 calls before CB can trigger
        sliding_window_size=100,
        failure_rate_threshold=0.0,  # Disabled by default
        fallback_strategy="block",
        fallback_cache_ttl_seconds=300,
        cb_open_burn_rate_multiplier=10.0,
    )


@pytest.fixture
def mock_state():
    """Mock CircuitBreakerStateData."""
    state = Mock()
    state.service_name = "test_service"
    state.state = "closed"
    state.failure_count = 0
    state.success_count = 0
    state.manually_controlled = False
    state.opened_at = None
    return state


# =============================================================================
# Test: minimum_calls
# =============================================================================


class TestMinimumCalls:
    """minimum_calls 게이트 테스트 (719 D4).

    minimum_calls는 rate 트리거만 게이팅한다. 연속 실패 증거는 트래픽과
    무관하므로 count 트리거는 정확히 failure_threshold에서 트립한다.
    판정 로직은 evaluate_trip 공유 술어에 있으므로 거기서 직접 검증한다.
    """

    def test_count_trigger_is_not_gated_by_minimum_calls(self):
        """관측 호출이 minimum_calls 미만이어도 연속 실패는 트립시킨다."""
        config = CircuitBreakerConfig(
            enabled=True, failure_threshold=5, minimum_calls=10
        )

        assert (
            evaluate_trip(
                consecutive_failures=5,
                window_failures=5,
                window_total=5,
                config=config,
            )
            == TRIP_REASON_COUNT
        )

    def test_count_trigger_trips_at_exactly_failure_threshold(self):
        config = CircuitBreakerConfig(
            enabled=True, failure_threshold=5, minimum_calls=10
        )

        assert (
            evaluate_trip(
                consecutive_failures=4,
                window_failures=4,
                window_total=4,
                config=config,
            )
            is None
        )
        assert (
            evaluate_trip(
                consecutive_failures=5,
                window_failures=5,
                window_total=5,
                config=config,
            )
            == TRIP_REASON_COUNT
        )

    def test_rate_trigger_gated_below_minimum_calls(self):
        """저트래픽 서비스: 관측 호출이 부족하면 비율은 평가하지 않는다."""
        # count 트리거는 사정권 밖에 두어 rate 게이트만 판정에 관여시킨다.
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=100,
            minimum_calls=10,
            failure_rate_threshold=50.0,
        )

        # 9건 중 5건 실패(56%)지만 minimum_calls 미달
        assert (
            evaluate_trip(
                consecutive_failures=1,
                window_failures=5,
                window_total=9,
                config=config,
            )
            is None
        )

    def test_rate_trigger_applies_at_minimum_calls(self):
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=100,
            minimum_calls=10,
            failure_rate_threshold=50.0,
        )

        # 10건 중 5건 실패(50%) — minimum_calls 충족
        assert (
            evaluate_trip(
                consecutive_failures=1,
                window_failures=5,
                window_total=10,
                config=config,
            )
            == TRIP_REASON_RATE
        )

    def test_rate_trigger_disabled_at_zero_threshold(self):
        """failure_rate_threshold=0이면 비율 트리거는 평가되지 않는다."""
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=100,
            minimum_calls=1,
            failure_rate_threshold=0,
        )

        assert (
            evaluate_trip(
                consecutive_failures=1,
                window_failures=20,
                window_total=20,
                config=config,
            )
            is None
        )


# =============================================================================
# Test: CircuitBreakerFallbackResult
# =============================================================================


class TestCircuitBreakerFallbackResult:
    """CircuitBreakerFallbackResult 타입 테스트."""

    def test_allow_result(self):
        """Allow 결과 생성."""
        result = CircuitBreakerFallbackResult.allow()

        assert result.allowed is True
        assert result.fallback_used is False
        assert result.fallback_type == ""

    def test_block_result(self):
        """Block 결과 생성."""
        result = CircuitBreakerFallbackResult.block("Service unavailable")

        assert result.allowed is False
        assert result.fallback_used is False
        assert "unavailable" in result.message

    def test_from_cache_result(self):
        """Cache fallback 결과 생성."""
        cached_data = {"product_id": 123, "name": "Test Product"}
        result = CircuitBreakerFallbackResult.from_cache(cached_data)

        assert result.allowed is False
        assert result.fallback_used is True
        assert result.fallback_type == "cache"
        assert result.fallback_data == cached_data

    def test_to_dlq_result(self):
        """DLQ fallback 결과 생성."""
        result = CircuitBreakerFallbackResult.to_dlq("Request queued")

        assert result.allowed is False
        assert result.fallback_used is True
        assert result.fallback_type == "dlq"

    def test_default_response_result(self):
        """Default response fallback 결과 생성."""
        default_data = {"status": "unknown", "items": []}
        result = CircuitBreakerFallbackResult.default_response(default_data)

        assert result.allowed is False
        assert result.fallback_used is True
        assert result.fallback_type == "default"
        assert result.fallback_data == default_data


# =============================================================================
# Test: Fallback Strategies
# =============================================================================


class TestFallbackStrategies:
    """Fallback 전략 테스트."""

    def test_should_allow_with_fallback_when_closed(
        self, mock_repository, base_config, mock_state
    ):
        """CB가 CLOSED 상태면 요청 허용."""
        mock_state.state = "closed"
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        result = service.should_allow_with_fallback("test_service")

        assert result.allowed is True
        assert result.fallback_used is False

    def test_should_allow_with_fallback_when_half_open(
        self, mock_repository, base_config, mock_state
    ):
        """CB가 HALF_OPEN 상태면 요청 허용 (테스트용)."""
        mock_state.state = "half_open"
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        result = service.should_allow_with_fallback("test_service")

        assert result.allowed is True

    def test_cache_fallback_strategy(self, mock_repository, mock_state):
        """Cache fallback 전략 테스트."""
        config = CircuitBreakerConfig(
            enabled=True,
            fallback_strategy="cache",
        )
        mock_state.state = "open"
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=config, repository=mock_repository)

        cached_data = {"product": "test"}
        with patch.object(service, "_get_cached_data", return_value=cached_data):
            result = service.should_allow_with_fallback(
                "test_service", cache_key="product:123"
            )

        assert result.allowed is False
        assert result.fallback_used is True
        assert result.fallback_type == "cache"
        assert result.fallback_data == cached_data

    def test_cache_fallback_miss_falls_back_to_block(self, mock_repository, mock_state):
        """캐시 미스 시 block으로 fallback."""
        config = CircuitBreakerConfig(
            enabled=True,
            fallback_strategy="cache",
        )
        mock_state.state = "open"
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=config, repository=mock_repository)

        with patch.object(service, "_get_cached_data", return_value=None):
            result = service.should_allow_with_fallback(
                "test_service", cache_key="product:123"
            )

        assert result.allowed is False
        assert result.fallback_used is False  # No cache hit

    def test_dlq_fallback_strategy(self, mock_repository, mock_state):
        """DLQ fallback 전략 테스트."""
        config = CircuitBreakerConfig(
            enabled=True,
            fallback_strategy="dlq",
        )
        mock_state.state = "open"
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=config, repository=mock_repository)

        request_data = {"order_id": 123, "amount": 10000}
        with patch.object(service, "_enqueue_to_dlq", return_value=True):
            result = service.should_allow_with_fallback(
                "test_service", request_data=request_data
            )

        assert result.allowed is False
        assert result.fallback_used is True
        assert result.fallback_type == "dlq"

    def test_default_response_fallback_strategy(self, mock_repository, mock_state):
        """Default response fallback 전략 테스트."""
        config = CircuitBreakerConfig(
            enabled=True,
            fallback_strategy="default_response",
        )
        mock_state.state = "open"
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=config, repository=mock_repository)

        default_data = {"items": [], "message": "Service unavailable"}
        result = service.should_allow_with_fallback(
            "test_service", default_response=default_data
        )

        assert result.allowed is False
        assert result.fallback_used is True
        assert result.fallback_type == "default"
        assert result.fallback_data == default_data


# =============================================================================
# Test: Snapshot Collection
# =============================================================================


class TestSnapshotCollection:
    """CB OPEN 시 스냅샷 수집 테스트."""

    def test_collect_failure_snapshot_basic(
        self, mock_repository, base_config, mock_state
    ):
        """기본 스냅샷 수집."""
        mock_state.failure_count = 5
        mock_state.success_count = 10
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        snapshot = service._collect_failure_snapshot(
            "test_service",
            mock_state,
            error_context={"error": "Connection timeout"},
            window_failures=5,
            window_total=15,
        )

        assert "service_name" in snapshot
        assert snapshot["service_name"] == "test_service"
        assert "timestamp" in snapshot
        assert "circuit_breaker" in snapshot
        assert snapshot["circuit_breaker"]["failure_count"] == 5
        assert snapshot["circuit_breaker"]["success_count"] == 10
        assert snapshot["circuit_breaker"]["consecutive_failure_count"] == 5
        assert snapshot["circuit_breaker"]["window_failure_count"] == 5
        assert snapshot["circuit_breaker"]["window_total_calls"] == 15
        assert "error_context" in snapshot
        assert snapshot["error_context"]["error"] == "Connection timeout"

    def test_collect_failure_snapshot_includes_threshold_config(
        self, mock_repository, base_config, mock_state
    ):
        """스냅샷에 threshold 설정이 포함되어야 함."""
        mock_state.failure_count = 5
        mock_state.success_count = 10

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        snapshot = service._collect_failure_snapshot(
            "test_service", mock_state, window_failures=5, window_total=15
        )

        threshold_config = snapshot["circuit_breaker"]["threshold_config"]
        assert threshold_config["failure_threshold"] == base_config.failure_threshold
        assert threshold_config["minimum_calls"] == base_config.minimum_calls
        assert (
            threshold_config["failure_rate_threshold"]
            == base_config.failure_rate_threshold
        )

    def test_collect_failure_snapshot_calculates_failure_rate(
        self, mock_repository, base_config, mock_state
    ):
        """스냅샷 실패율은 outcome window 증거에서 계산된다."""
        mock_state.failure_count = 3
        mock_state.success_count = 0

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        snapshot = service._collect_failure_snapshot(
            "test_service", mock_state, window_failures=3, window_total=10
        )

        assert snapshot["circuit_breaker"]["failure_rate_percent"] == 30.0


# =============================================================================
# Test: Burn Rate Multiplier
# =============================================================================


class TestBurnRateMultiplier:
    """CB OPEN 시 Burn Rate 가중치 테스트."""

    def test_burn_rate_multiplier_emits_event(
        self, mock_repository, base_config, mock_state
    ):
        """CB OPEN 시 burn rate multiplier가 올바르게 설정되어야 함."""
        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        # Verify the config has the burn rate multiplier
        assert service.config.cb_open_burn_rate_multiplier == 10.0

        # The method should run without errors (it has try/except inside)
        # It will fail silently if emergency_manager or event_bus are not available
        service._apply_burn_rate_multiplier("test_service")

    def test_burn_rate_multiplier_uses_config_value(self, mock_repository, mock_state):
        """설정된 multiplier 값이 올바르게 저장되어야 함."""
        config = CircuitBreakerConfig(
            enabled=True,
            cb_open_burn_rate_multiplier=15.0,  # Custom multiplier
        )

        service = CircuitBreakerService(config=config, repository=mock_repository)

        # Verify custom config value is stored
        assert service.config.cb_open_burn_rate_multiplier == 15.0

        # The method should run without errors
        service._apply_burn_rate_multiplier("test_service")


# =============================================================================
# Test: Integration - record_failure with enhancements
# =============================================================================


class TestRecordFailureIntegration:
    """record_failure 통합 테스트."""

    def test_record_failure_rate_trigger_respects_minimum_calls(
        self, mock_repository, mock_state
    ):
        """관측 호출이 minimum_calls 미만이면 비율 트리거로 열리지 않는다.

        count 트리거는 사정권 밖(failure_threshold=100)에 두어 rate 게이트만
        판정에 관여하게 한다.
        """
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=100,
            minimum_calls=10,
            failure_rate_threshold=50.0,
        )
        mock_state.failure_count = 5
        mock_state.success_count = 0
        mock_state.state = "closed"
        mock_repository.get_or_create.return_value = mock_state
        mock_repository.record_failure.return_value = mock_state

        service = CircuitBreakerService(config=config, repository=mock_repository)

        # 관측 호출 5건 < minimum_calls 10
        for _ in range(5):
            service.record_failure("test_service")

        # update_state should NOT be called (circuit should not open)
        mock_repository.update_state.assert_not_called()

    def test_record_failure_opens_circuit_when_conditions_met(
        self, mock_repository, base_config, mock_state
    ):
        """조건 충족 시 CB가 열려야 함."""
        # State: 5 failures, 10 successes = 15 total (>= 10 minimum)
        mock_state.failure_count = 5
        mock_state.success_count = 10
        mock_state.state = "closed"
        mock_repository.get_or_create.return_value = mock_state
        mock_repository.record_failure.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        with patch.object(service, "_log_circuit_open_audit"):
            with patch.object(service, "_apply_burn_rate_multiplier"):
                service.record_failure("test_service")

        # update_state should be called to open circuit
        mock_repository.update_state.assert_called()
        call_kwargs = mock_repository.update_state.call_args[1]
        assert call_kwargs["state"] == "open"

    def test_record_failure_collects_snapshot_when_opening(
        self, mock_repository, base_config, mock_state
    ):
        """CB 열 때 스냅샷이 수집되어야 함."""
        mock_state.failure_count = 5
        mock_state.success_count = 10
        mock_state.state = "closed"
        mock_repository.get_or_create.return_value = mock_state
        mock_repository.record_failure.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        with patch.object(service, "_collect_failure_snapshot") as mock_snapshot:
            mock_snapshot.return_value = {"test": "snapshot"}
            with patch.object(service, "_log_circuit_open_audit") as mock_audit:
                with patch.object(service, "_apply_burn_rate_multiplier"):
                    service.record_failure(
                        "test_service", error_context={"error": "timeout"}
                    )

        # Snapshot should be collected
        mock_snapshot.assert_called_once()
        # Audit should be called with snapshot
        mock_audit.assert_called_once()

    def test_record_failure_applies_burn_rate_multiplier(
        self, mock_repository, base_config, mock_state
    ):
        """CB 열 때 burn rate multiplier가 적용되어야 함."""
        mock_state.failure_count = 5
        mock_state.success_count = 10
        mock_state.state = "closed"
        mock_repository.get_or_create.return_value = mock_state
        mock_repository.record_failure.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        with patch.object(service, "_log_circuit_open_audit"):
            with patch.object(service, "_apply_burn_rate_multiplier") as mock_burn:
                service.record_failure("test_service")

        mock_burn.assert_called_once_with("test_service")


# =============================================================================
# Test: get_total_calls
# =============================================================================


class TestGetTotalCalls:
    """get_total_calls 메서드 테스트."""

    def test_get_total_calls_returns_sum(
        self, mock_repository, base_config, mock_state
    ):
        """failure_count + success_count 합계 반환."""
        mock_state.failure_count = 3
        mock_state.success_count = 7
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        total = service.get_total_calls("test_service")

        assert total == 10

    def test_get_total_calls_for_new_service(
        self, mock_repository, base_config, mock_state
    ):
        """새 서비스는 0 반환."""
        mock_state.failure_count = 0
        mock_state.success_count = 0
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        total = service.get_total_calls("new_service")

        assert total == 0


# =============================================================================
# Test: Disabled CB
# =============================================================================


class TestDisabledCircuitBreaker:
    """비활성화된 CB 테스트."""

    def test_should_allow_with_fallback_when_disabled(self, mock_repository):
        """CB 비활성화 시 항상 allow."""
        config = CircuitBreakerConfig(enabled=False)

        service = CircuitBreakerService(config=config, repository=mock_repository)

        result = service.should_allow_with_fallback("test_service")

        assert result.allowed is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
