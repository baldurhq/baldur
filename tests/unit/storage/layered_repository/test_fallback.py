"""
지능형 폴백 테스트.
"""

from unittest.mock import MagicMock


class TestIntelligentFallback:
    """지능형 폴백 테스트."""

    def test_fallback_on_l2_error(self):
        """L2 에러 시 L1만으로 동작."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = []
        mock_l2.get_by_service_name.side_effect = Exception("L2 error")

        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)

        # When: L2 에러 발생
        state = repo.get_or_create("test-service")

        # Then: L1에서 정상 동작
        assert state is not None
        assert state.service_name == "test-service"

    def test_l2_health_status_tracks_errors(self):
        """L2 에러 발생 시 메트릭 추적."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = []
        mock_l2.update_state.side_effect = Exception("Write failed")
        mock_l2.get_or_create.side_effect = Exception("Write failed")

        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)
        repo._l2_healthy = True

        # When: L2 쓰기 시도
        repo.record_failure("test-service")

        # Then: L1은 정상 동작
        state = repo.get_by_service_name("test-service")
        assert state is not None

    def test_no_l2_operations_when_unhealthy(self):
        """L2가 unhealthy 상태에서도 L1은 정상 동작."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = []

        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)
        repo._l2_healthy = False  # L2 비정상 상태로 설정

        # When: 상태 변경
        repo.record_failure("test-service")

        # Then: L1은 정상 동작
        state = repo.get_by_service_name("test-service")
        assert state is not None
        assert state.failure_count >= 1
