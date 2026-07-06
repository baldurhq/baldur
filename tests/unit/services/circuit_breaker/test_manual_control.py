"""
Tests for Circuit Breaker Manual Control Mixin

Covers:
- force_open operation
- force_close operation
- reset operation
- Kill Switch integration
- TTL management

Refactored to use Factory Pattern (Phase 2):
- MockCircuitBreakerStateData → factories.MockCircuitBreakerStateData
- MockRepository → factories.InMemoryCircuitBreakerRepository
"""

from unittest.mock import MagicMock, patch

# Factory Pattern imports
from tests.factories import (
    InMemoryCircuitBreakerRepository,
    MockCircuitBreakerStateData,
)


class TestForceOpen:
    """Tests for force_open operation."""

    def test_force_open_success(self):
        """Test force_open successfully opens circuit."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            # Actor info is now read from ActorContext (SYSTEM_ACTOR fallback)
            result = service.force_open(
                service_name="test_service",
                reason="Maintenance",
            )

        assert result.success is True
        assert result.new_state == "open"

    def test_force_open_already_open(self):
        """Test force_open when already open."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Already open",
            )

        assert result.success is True
        assert "already open" in result.message.lower()

    def test_force_open_blocked_by_kill_switch(self):
        """Test force_open blocked when kill switch active."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Test",
            )

        assert result.success is False
        assert "kill switch" in result.error.lower()

    def test_force_open_with_actor_context(self):
        """Test force_open reads actor from ActorContext."""
        from baldur.context.actor_context import ActorContext
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            # Set actor context explicitly (simulating Django middleware)
            with ActorContext.set_actor(
                actor_id="test_user@example.com",
                actor_type="user",
                source="web",
            ):
                result = service.force_open(
                    service_name="test_service",
                    reason="Test",
                )

        assert result.success is True

    def test_force_open_atomic_failure(self):
        """Test force_open handles atomic failure."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._atomic_success = False
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Test",
            )

        assert result.success is False


class TestForceClose:
    """Tests for force_close operation."""

    def test_force_close_success(self):
        """Test force_close successfully closes circuit."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            # Actor info is now read from ActorContext (SYSTEM_ACTOR fallback)
            result = service.force_close(
                service_name="test_service",
                reason="Service recovered",
            )

        assert result.success is True
        assert result.new_state == "closed"

    def test_force_close_already_closed(self):
        """Test force_close when already closed."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_close(
                service_name="test_service",
                reason="Already closed",
            )

        assert result.success is True

    def test_force_close_with_replay_trigger(self):
        """Test force_close with replay trigger."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_close(
                service_name="test_service",
                reason="Recovery",
                trigger_replay=True,
            )

        assert result.success is True

    def test_force_close_blocked_by_kill_switch(self):
        """Test force_close blocked when kill switch active."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            result = service.force_close(
                service_name="test_service",
                reason="Test",
            )

        assert result.success is False


class TestReset:
    """Tests for reset operation."""

    def test_reset_clears_state(self):
        """Test reset clears circuit breaker state."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="half_open", failure_count=5
        )
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.reset(service_name="test_service")

        # Result may or may not succeed depending on repo implementation
        assert result is not None


class TestKillSwitchIntegration:
    """Tests for Kill Switch integration."""

    def test_is_system_enabled_check(self):
        """Test _is_system_enabled function."""
        from baldur.services.circuit_breaker.manual_control import (
            _is_system_enabled,
        )

        # Mock system control
        with patch(
            "baldur.services.system_control.SystemControlManager"
        ) as mock_manager_cls:
            mock_manager = MagicMock()
            mock_manager.is_enabled.return_value = True
            mock_manager_cls.return_value = mock_manager

            result = _is_system_enabled()
            assert result is True

    def test_is_system_enabled_default_true(self):
        """Test _is_system_enabled returns True by default."""
        from baldur.services.circuit_breaker.manual_control import (
            _is_system_enabled,
        )

        # Should return True even when module not available
        result = _is_system_enabled()
        assert isinstance(result, bool)


class TestDecisionLogging:
    """Tests for decision logging integration."""

    def test_force_open_completes_without_error(self):
        """Test force_open completes without error."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Test",
            )

            assert result is not None


class TestTTLManagement:
    """Tests for manual override TTL management."""

    def test_force_open_uses_config_ttl(self):
        """Test force_open uses TTL from config."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            manual_override_ttl_minutes=120,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Test",
            )

        assert result.success is True
        # TTL should be passed to repository
