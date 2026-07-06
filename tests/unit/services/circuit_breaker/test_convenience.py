"""
Tests for Circuit Breaker Convenience Functions

Covers:
- Module-level convenience functions
- Singleton access

Refactored to use Factory Pattern (Phase 2):
- MockCircuitBreakerStateData → factories.MockCircuitBreakerStateData
- MockRepository → factories.InMemoryCircuitBreakerRepository
"""

from unittest.mock import patch

# Factory Pattern imports
from tests.factories import (
    InMemoryCircuitBreakerRepository,
    MockCircuitBreakerStateData,
)


class TestGetCircuitBreakerService:
    """Tests for get_circuit_breaker_service."""

    def test_returns_service_instance(self):
        """Test get_circuit_breaker_service returns service instance."""
        from baldur.services.circuit_breaker.convenience import (
            get_circuit_breaker_service,
        )
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        service = get_circuit_breaker_service()
        assert isinstance(service, CircuitBreakerService)

    def test_returns_singleton(self):
        """Test get_circuit_breaker_service returns singleton."""
        from baldur.services.circuit_breaker.convenience import (
            get_circuit_breaker_service,
        )

        service1 = get_circuit_breaker_service()
        service2 = get_circuit_breaker_service()

        assert service1 is service2


class TestShouldAllowRequest:
    """Tests for should_allow_request convenience function."""

    def test_should_allow_request_when_closed(self):
        """Test should_allow_request returns True when closed."""
        from baldur.services.circuit_breaker import convenience
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        # Reset singleton and set up mock
        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_service = CircuitBreakerService(config=config, repository=mock_repo)

        convenience.configure_circuit_breaker_service(mock_service)

        try:
            result = convenience.should_allow_request("test_service")
            assert result is True
        finally:
            convenience.reset_circuit_breaker_service()

    def test_should_allow_request_when_open(self):
        """Test should_allow_request returns False when open."""
        from baldur.services.circuit_breaker import convenience
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )
        mock_service = CircuitBreakerService(config=config, repository=mock_repo)

        convenience.configure_circuit_breaker_service(mock_service)

        try:
            result = convenience.should_allow_request("test_service")
            assert result is False
        finally:
            convenience.reset_circuit_breaker_service()


class TestForceOpenCircuit:
    """Tests for force_open_circuit convenience function."""

    def test_force_open_circuit_success(self):
        """Test force_open_circuit successfully opens."""
        from baldur.services.circuit_breaker import convenience
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_service = CircuitBreakerService(config=config, repository=mock_repo)

        convenience.configure_circuit_breaker_service(mock_service)

        try:
            with patch(
                "baldur.services.circuit_breaker.manual_control._is_system_enabled",
                return_value=True,
            ):
                result = convenience.force_open_circuit(
                    service_name="test_service",
                    reason="Test",
                )
            assert result.success is True
        finally:
            convenience.reset_circuit_breaker_service()


class TestForceCloseCircuit:
    """Tests for force_close_circuit convenience function."""

    def test_force_close_circuit_success(self):
        """Test force_close_circuit successfully closes."""
        from baldur.services.circuit_breaker import convenience
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )
        mock_service = CircuitBreakerService(config=config, repository=mock_repo)

        convenience.configure_circuit_breaker_service(mock_service)

        try:
            with patch(
                "baldur.services.circuit_breaker.manual_control._is_system_enabled",
                return_value=True,
            ):
                result = convenience.force_close_circuit(
                    service_name="test_service",
                    reason="Recovery",
                )
            assert result.success is True
        finally:
            convenience.reset_circuit_breaker_service()

    def test_force_close_circuit_with_replay(self):
        """Test force_close_circuit with replay trigger."""
        from baldur.services.circuit_breaker import convenience
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_service = CircuitBreakerService(config=config, repository=mock_repo)

        convenience.configure_circuit_breaker_service(mock_service)

        try:
            with patch(
                "baldur.services.circuit_breaker.manual_control._is_system_enabled",
                return_value=True,
            ):
                result = convenience.force_close_circuit(
                    service_name="test_service",
                    reason="Recovery",
                    trigger_replay=True,
                )
            assert result.success is True
        finally:
            convenience.reset_circuit_breaker_service()


class TestRecordRateLimit:
    """Tests for record_rate_limit convenience function."""

    def test_record_rate_limit(self):
        """Test record_rate_limit convenience function."""
        from baldur.services.circuit_breaker import convenience
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_service = CircuitBreakerService(config=config, repository=mock_repo)

        convenience.configure_circuit_breaker_service(mock_service)

        try:
            # Should not raise
            result = convenience.record_rate_limit("test_service")
            # Result is None if no cascade detected
            assert result is None or result.success is True
        finally:
            convenience.reset_circuit_breaker_service()
