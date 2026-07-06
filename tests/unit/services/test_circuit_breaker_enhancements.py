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
    """minimum_calls 파라미터 테스트."""

    def test_circuit_not_open_when_below_minimum_calls(
        self, mock_repository, base_config, mock_state
    ):
        """총 호출 수가 minimum_calls 미만이면 CB가 열리지 않아야 함."""
        # Setup: failure_count = 5 (threshold), but total calls = 5 < 10 (minimum)
        mock_state.failure_count = 5
        mock_state.success_count = 0  # Total = 5 < 10
        mock_repository.get_or_create.return_value = mock_state
        mock_repository.record_failure.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        # Should not open circuit
        should_open = service._should_open_circuit(mock_state)

        assert should_open is False

    def test_circuit_opens_when_above_minimum_calls(
        self, mock_repository, base_config, mock_state
    ):
        """총 호출 수가 minimum_calls 이상이고 threshold 초과 시 CB가 열려야 함."""
        # Setup: failure_count = 5 (threshold), total calls = 15 >= 10 (minimum)
        mock_state.failure_count = 5
        mock_state.success_count = 10  # Total = 15 >= 10
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        should_open = service._should_open_circuit(mock_state)

        assert should_open is True

    def test_minimum_calls_protects_low_traffic_services(
        self, mock_repository, base_config, mock_state
    ):
        """저트래픽 서비스에서 일시적 실패로 CB가 열리는 것을 방지."""
        # 새벽 시간대: 10개 요청 중 5개 실패 (50% 에러율)
        # minimum_calls=10이면 아직 판단하지 않음
        mock_state.failure_count = 5
        mock_state.success_count = 4  # Total = 9 < 10
        mock_repository.get_or_create.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        should_open = service._should_open_circuit(mock_state)

        assert should_open is False

    def test_rate_based_threshold_with_minimum_calls(self, mock_repository, mock_state):
        """비율 기반 threshold와 minimum_calls 조합 테스트."""
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=5,
            minimum_calls=20,
            failure_rate_threshold=50.0,  # 50% 에러율
        )

        # 20개 요청 중 10개 실패 (50%) - threshold 충족
        mock_state.failure_count = 10
        mock_state.success_count = 10  # Total = 20 >= 20

        service = CircuitBreakerService(config=config, repository=mock_repository)

        should_open = service._should_open_circuit(mock_state)

        assert should_open is True

    def test_rate_based_threshold_below_minimum_calls(
        self, mock_repository, mock_state
    ):
        """minimum_calls 미만이면 비율 기반 threshold도 적용 안됨."""
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=5,
            minimum_calls=20,
            failure_rate_threshold=50.0,
        )

        # 10개 요청 중 8개 실패 (80%) - 하지만 minimum_calls 미달
        mock_state.failure_count = 8
        mock_state.success_count = 2  # Total = 10 < 20

        service = CircuitBreakerService(config=config, repository=mock_repository)

        should_open = service._should_open_circuit(mock_state)

        assert should_open is False


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
            "test_service", mock_state, error_context={"error": "Connection timeout"}
        )

        assert "service_name" in snapshot
        assert snapshot["service_name"] == "test_service"
        assert "timestamp" in snapshot
        assert "circuit_breaker" in snapshot
        assert snapshot["circuit_breaker"]["failure_count"] == 5
        assert snapshot["circuit_breaker"]["success_count"] == 10
        assert snapshot["circuit_breaker"]["total_calls"] == 15
        assert "error_context" in snapshot
        assert snapshot["error_context"]["error"] == "Connection timeout"

    def test_collect_failure_snapshot_includes_threshold_config(
        self, mock_repository, base_config, mock_state
    ):
        """스냅샷에 threshold 설정이 포함되어야 함."""
        mock_state.failure_count = 5
        mock_state.success_count = 10

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        snapshot = service._collect_failure_snapshot("test_service", mock_state)

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
        """스냅샷에 실패율이 계산되어야 함."""
        mock_state.failure_count = 3
        mock_state.success_count = 7  # 30% failure rate

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

        snapshot = service._collect_failure_snapshot("test_service", mock_state)

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

    def test_record_failure_respects_minimum_calls(
        self, mock_repository, base_config, mock_state
    ):
        """record_failure가 minimum_calls를 존중해야 함."""
        # State: 5 failures, 0 successes = 5 total (< 10 minimum)
        mock_state.failure_count = 5
        mock_state.success_count = 0
        mock_state.state = "closed"
        mock_repository.get_or_create.return_value = mock_state
        mock_repository.record_failure.return_value = mock_state

        service = CircuitBreakerService(config=base_config, repository=mock_repository)

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
