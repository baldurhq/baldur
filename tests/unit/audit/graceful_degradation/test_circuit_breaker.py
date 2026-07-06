"""
HashChainCircuitBreaker 테스트.

Circuit breaker for hash chain operations 테스트.
"""

import time


class TestHashChainCircuitBreaker:
    """Tests for HashChainCircuitBreaker."""

    def test_initial_state_closed(self):
        """Test circuit starts in closed state."""
        from baldur.audit.graceful_degradation import (
            CircuitState,
            HashChainCircuitBreaker,
        )

        cb = HashChainCircuitBreaker()

        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute() is True

    def test_opens_after_threshold(self):
        """Test circuit opens after failure threshold."""
        from baldur.audit.graceful_degradation import (
            CircuitState,
            HashChainCircuitBreaker,
            HashChainCircuitBreakerConfig,
        )

        config = HashChainCircuitBreakerConfig(failure_threshold=3)
        cb = HashChainCircuitBreaker(config=config)

        for _ in range(3):
            cb.record_failure()

        assert cb.state == CircuitState.OPEN
        assert cb.can_execute() is False

    def test_half_open_after_timeout(self):
        """Test circuit transitions to half-open after timeout."""
        from baldur.audit.graceful_degradation import (
            CircuitState,
            HashChainCircuitBreaker,
            HashChainCircuitBreakerConfig,
        )

        config = HashChainCircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.1,
        )
        cb = HashChainCircuitBreaker(config=config)

        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        time.sleep(0.15)

        # Should transition to HALF_OPEN on next check
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_allows_limited_requests(self):
        """Test half-open allows limited requests."""
        from baldur.audit.graceful_degradation import (
            HashChainCircuitBreaker,
            HashChainCircuitBreakerConfig,
        )

        config = HashChainCircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.1,
            half_open_requests=2,
        )
        cb = HashChainCircuitBreaker(config=config)

        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)

        # First 2 requests should be allowed
        assert cb.can_execute() is True
        assert cb.can_execute() is True
        # Third should be blocked
        assert cb.can_execute() is False

    def test_closes_on_success(self):
        """Test circuit closes after successful requests in half-open."""
        from baldur.audit.graceful_degradation import (
            CircuitState,
            HashChainCircuitBreaker,
            HashChainCircuitBreakerConfig,
        )

        config = HashChainCircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.1,
            success_threshold=2,
        )
        cb = HashChainCircuitBreaker(config=config)

        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)

        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        cb.record_success()

        assert cb.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self):
        """Test circuit reopens on failure in half-open state."""
        from baldur.audit.graceful_degradation import (
            CircuitState,
            HashChainCircuitBreaker,
            HashChainCircuitBreakerConfig,
        )

        config = HashChainCircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.1,
        )
        cb = HashChainCircuitBreaker(config=config)

        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)

        assert cb.state == CircuitState.HALF_OPEN

        cb.record_failure()

        assert cb.state == CircuitState.OPEN

    def test_force_open(self):
        """Test force open."""
        from baldur.audit.graceful_degradation import (
            CircuitState,
            HashChainCircuitBreaker,
        )

        cb = HashChainCircuitBreaker()

        cb.force_open()

        assert cb.state == CircuitState.OPEN

    def test_force_closed(self):
        """Test force closed."""
        from baldur.audit.graceful_degradation import (
            CircuitState,
            HashChainCircuitBreaker,
            HashChainCircuitBreakerConfig,
        )

        config = HashChainCircuitBreakerConfig(failure_threshold=1)
        cb = HashChainCircuitBreaker(config=config)

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        cb.force_closed()

        assert cb.state == CircuitState.CLOSED

    def test_stats(self):
        """Test statistics tracking."""
        from baldur.audit.graceful_degradation import HashChainCircuitBreaker

        cb = HashChainCircuitBreaker()

        cb.can_execute()
        cb.record_success()
        cb.record_failure()

        stats = cb.get_stats()

        assert stats["total_requests"] == 1
        assert stats["total_successes"] == 1
        assert stats["total_failures"] == 1

    def test_notifies_degradation_manager(self):
        """Test circuit breaker notifies degradation manager."""
        from baldur.audit.graceful_degradation import (
            CircuitState,
            DegradationLevel,
            HashChainCircuitBreaker,
            HashChainCircuitBreakerConfig,
            HashChainDegradationManager,
        )

        from .conftest import MockRedisClient

        HashChainDegradationManager.reset_instance()

        mock_redis = MockRedisClient()
        degradation_mgr = HashChainDegradationManager(redis_client=mock_redis)

        config = HashChainCircuitBreakerConfig(failure_threshold=2)
        cb = HashChainCircuitBreaker(
            config=config,
            degradation_manager=degradation_mgr,
        )

        cb.record_failure()
        cb.record_failure()

        # Circuit should be open and degradation manager notified
        assert cb.state == CircuitState.OPEN
        assert degradation_mgr.level == DegradationLevel.DEGRADED

        HashChainDegradationManager.reset_instance()
