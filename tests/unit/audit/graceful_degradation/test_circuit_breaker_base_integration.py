"""
HashChainCircuitBreaker refactored base integration tests (307).

Tests the new behavior after HashChainCircuitBreaker was refactored
to inherit from CircuitBreakerBase: _on_state_changed hook,
_can_attempt_half_open limiting, _on_close recovery notification,
and DegradationBroadcaster integration in HashChainDegradationManager.
"""

from unittest.mock import MagicMock, patch

from baldur.audit.graceful_degradation.circuit_breaker import (
    HashChainCircuitBreaker,
)
from baldur.audit.graceful_degradation.enums import (
    CircuitState,
    HashChainCircuitBreakerConfig,
)


class TestHashChainCircuitBreakerBaseIntegrationBehavior:
    """HashChainCircuitBreaker behavior via CircuitBreakerBase inheritance."""

    @patch(
        "baldur.audit.resilience.metrics.AuditMetrics",
        autospec=True,
    )
    def test_on_state_changed_sets_circuit_state_metric(self, mock_metrics_cls):
        """DR-6: State transition updates AuditMetrics with 'redis_hashchain' key."""
        mock_instance = MagicMock()
        mock_metrics_cls.get_instance.return_value = mock_instance

        config = HashChainCircuitBreakerConfig(failure_threshold=1)
        cb = HashChainCircuitBreaker(config=config)
        cb.record_failure()

        mock_instance.set_circuit_state.assert_called_with(
            "redis_hashchain",
            "open",
        )

    def test_can_attempt_half_open_limits_requests(self):
        """_can_attempt_half_open blocks after half_open_max_requests."""
        config = HashChainCircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout_seconds=0.01,
            half_open_requests=2,
        )
        cb = HashChainCircuitBreaker(config=config)
        cb.record_failure()
        cb._last_failure_mono -= 0.02

        assert cb.can_execute() is True
        assert cb.can_execute() is True
        assert cb.can_execute() is False

    def test_half_open_requests_reset_on_transition_to_half_open(self):
        """_half_open_requests resets to 0 when transitioning to HALF_OPEN."""
        config = HashChainCircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout_seconds=0.01,
            half_open_requests=1,
            success_threshold=1,
        )
        cb = HashChainCircuitBreaker(config=config)

        # First cycle: CLOSED -> OPEN -> HALF_OPEN -> CLOSED
        cb.record_failure()
        cb._last_failure_mono -= 0.02
        cb.can_execute()
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

        # Second cycle: CLOSED -> OPEN -> HALF_OPEN
        cb.record_failure()
        cb._last_failure_mono -= 0.02

        # Should allow request again (counter was reset)
        assert cb.can_execute() is True

    def test_on_close_notifies_degradation_manager_recovery(self):
        """_on_close calls degradation_manager.on_redis_recovery."""
        mock_dm = MagicMock()
        config = HashChainCircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout_seconds=0.01,
            success_threshold=1,
        )
        cb = HashChainCircuitBreaker(config=config, degradation_manager=mock_dm)

        cb.record_failure()
        cb._last_failure_mono -= 0.02
        cb.can_execute()
        cb.record_success()

        mock_dm.on_redis_recovery.assert_called_once()

    def test_record_failure_notifies_degradation_manager_when_open(self):
        """record_failure notifies degradation_manager.on_redis_failure when OPEN."""
        mock_dm = MagicMock()
        config = HashChainCircuitBreakerConfig(failure_threshold=2)
        cb = HashChainCircuitBreaker(config=config, degradation_manager=mock_dm)

        cb.record_failure()
        mock_dm.on_redis_failure.assert_not_called()

        cb.record_failure()
        mock_dm.on_redis_failure.assert_called_once()

    def test_force_closed_resets_half_open_requests(self):
        """force_closed resets _half_open_requests to 0."""
        config = HashChainCircuitBreakerConfig(failure_threshold=1)
        cb = HashChainCircuitBreaker(config=config)
        cb.record_failure()
        cb._half_open_requests = 5

        cb.force_closed()
        assert cb.state == CircuitState.CLOSED
        assert cb._half_open_requests == 0

    def test_get_stats_includes_config(self):
        """get_stats includes config dict with HashChain-specific keys."""
        config = HashChainCircuitBreakerConfig(
            failure_threshold=5,
            recovery_timeout_seconds=60.0,
            half_open_requests=3,
            success_threshold=2,
        )
        cb = HashChainCircuitBreaker(config=config)
        stats = cb.get_stats()

        assert "config" in stats
        assert stats["config"]["failure_threshold"] == 5
        assert stats["config"]["recovery_timeout_seconds"] == 60.0
        assert stats["config"]["half_open_requests"] == 3
        assert stats["config"]["success_threshold"] == 2


class TestHashChainDegradationManagerBroadcastBehavior:
    """HashChainDegradationManager broadcasts via DegradationBroadcaster on set_level."""

    def setup_method(self):
        from baldur.audit.graceful_degradation import HashChainDegradationManager

        HashChainDegradationManager.reset_instance()

    def teardown_method(self):
        from baldur.audit.graceful_degradation import HashChainDegradationManager

        HashChainDegradationManager.reset_instance()

    @patch(
        "baldur.audit.resilience.degradation_protocol.DegradationBroadcaster",
        autospec=True,
    )
    def test_set_level_broadcasts_degradation(self, mock_broadcaster):
        """set_level broadcasts state change with source='redis_hashchain'."""
        from baldur.audit.graceful_degradation import (
            DegradationLevel,
            HashChainDegradationManager,
        )

        from .conftest import MockRedisClient

        manager = HashChainDegradationManager(redis_client=MockRedisClient())
        manager.set_level(DegradationLevel.DEGRADED, "test failure")

        mock_broadcaster.notify.assert_called_once_with(
            "redis_hashchain",
            True,
            "degraded",
            "test failure",
        )

    @patch(
        "baldur.audit.resilience.degradation_protocol.DegradationBroadcaster",
        autospec=True,
    )
    def test_set_level_broadcasts_recovery(self, mock_broadcaster):
        """set_level to NORMAL broadcasts is_degraded=False."""
        from baldur.audit.graceful_degradation import (
            DegradationLevel,
            HashChainDegradationManager,
        )

        from .conftest import MockRedisClient

        manager = HashChainDegradationManager(redis_client=MockRedisClient())
        manager.set_level(DegradationLevel.DEGRADED, "initial")
        mock_broadcaster.reset_mock()

        manager.set_level(DegradationLevel.NORMAL, "redis_recovered")

        mock_broadcaster.notify.assert_called_once_with(
            "redis_hashchain",
            False,
            "normal",
            "redis_recovered",
        )

    @patch(
        "baldur.audit.resilience.degradation_protocol.DegradationBroadcaster",
        autospec=True,
    )
    def test_set_same_level_does_not_broadcast(self, mock_broadcaster):
        """set_level with same level is no-op (no broadcast)."""
        from baldur.audit.graceful_degradation import (
            DegradationLevel,
            HashChainDegradationManager,
        )

        from .conftest import MockRedisClient

        manager = HashChainDegradationManager(redis_client=MockRedisClient())
        # Already NORMAL, setting NORMAL again
        manager.set_level(DegradationLevel.NORMAL, "no change")

        mock_broadcaster.notify.assert_not_called()
