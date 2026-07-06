"""
L2 헬스체크 테스트.
"""

from unittest.mock import MagicMock


class TestL2HealthCheck:
    """L2 헬스체크 테스트."""

    def test_get_l2_health(self):
        """L2 헬스 정보 조회."""
        from baldur.adapters.memory.circuit_breaker import (
            LayeredCircuitBreakerStateRepository,
        )

        repo = LayeredCircuitBreakerStateRepository(l2_repo=None)
        health = repo.get_l2_health()

        assert "healthy" in health
        assert "consecutive_failures" in health
        assert "last_error_time" in health
        assert "adapter_type" in health
        assert "timeout_ms" in health

    def test_reset_l2_health(self):
        """L2 헬스 상태 리셋."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = []

        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)
        repo._l2_healthy = False
        repo._l2_consecutive_failures = 5

        # When: 헬스 리셋
        repo.reset_l2_health()

        # Then: 헬시 상태로 복구
        assert repo._l2_healthy is True
        assert repo._l2_consecutive_failures == 0

    def test_consecutive_failures_tracked(self):
        """연속 실패 횟수 추적 기본 동작."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
            LayeredCircuitBreakerStateRepository,
        )

        mock_l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        mock_l2.get_all_states.return_value = []

        repo = LayeredCircuitBreakerStateRepository(l2_repo=mock_l2)

        # L2 헬스 상태 확인
        health = repo.get_l2_health()
        assert "consecutive_failures" in health
        assert health["consecutive_failures"] >= 0
