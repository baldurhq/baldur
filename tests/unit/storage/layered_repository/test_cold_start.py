"""
콜드 스타트 보호 테스트.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock


class TestColdStartProtection:
    """콜드 스타트 보호 테스트."""

    def test_default_state_is_closed(self):
        """기본 상태가 CLOSED (안전한 값)."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        state = repo.get_or_create("new-service")

        assert state.state == "closed", "기본 상태는 CLOSED여야 함"
        assert state.failure_count == 0
        assert state.success_count == 0

    def test_l2_load_attempted_on_init(self):
        """초기화 시 L2 로드 시도."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.interfaces.repositories import CircuitBreakerStateData

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = [
            CircuitBreakerStateData(
                service_name="existing-service",
                state="open",
                failure_count=5,
                success_count=0,
                last_failure_at=datetime.now(UTC),
            )
        ]

        # When: LayeredRepository 초기화 시 L2에서 로드 시도
        LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)

        # Then: L2에서 상태 로드됨
        mock_l2.get_all_states.assert_called()

    def test_cold_start_with_failed_l2(self):
        """L2 로드 실패 시 기본값 사용."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.side_effect = Exception("L2 connection failed")

        # When: L2 연결 실패 상황에서 초기화
        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)

        # Then: 에러 없이 동작하고 기본값 사용
        state = repo.get_or_create("new-service")
        assert state.state == "closed"
