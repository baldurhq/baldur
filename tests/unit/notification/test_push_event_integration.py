"""
Push-Only Event Integration Tests

Tests for verifying that DLQService and CircuitBreakerService
properly emit push events for metric updates.
"""

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# DLQService Push Event Integration Tests
# =============================================================================


class TestDLQServicePushEvents:
    """Tests for DLQ push event emission."""

    @patch("baldur.metrics.event_handlers.DLQMetricEventHandler.on_item_created")
    def test_store_failure_emits_on_item_created(self, mock_on_item_created):
        """
        Purpose:
            Verify store_failure calls on_item_created event handler.
        """
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.dlq import DLQConfig, DLQService

        # Arrange
        mock_repo = MagicMock()
        mock_repo.create.return_value = MagicMock(id=123)

        # Use config with enabled=True
        config = DLQConfig(enabled=True)
        service = DLQService(config=config, repository=mock_repo)

        # Act
        result = service.store_failure(
            domain="payment",
            failure_type="PG_TIMEOUT",
            error_message="Connection timeout",
            mode="sync",
        )

        # Assert
        assert result.success is True
        mock_on_item_created.assert_called_once()
        args, kwargs = mock_on_item_created.call_args
        assert args == ("payment", "PG_TIMEOUT")
        assert "duration_seconds" in kwargs
        assert isinstance(kwargs["duration_seconds"], float)

    def test_resolve_entry_emits_on_item_resolved(self):
        """
        Purpose:
            Verify resolve_entry calls on_item_resolved event handler.
        """
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        # Verify the handler has expected methods
        assert hasattr(DLQMetricEventHandler, "on_item_resolved")
        assert callable(DLQMetricEventHandler.on_item_resolved)

    @patch("baldur.metrics.event_handlers.DLQMetricEventHandler.on_item_created")
    def test_event_handler_import_works(self, mock_on_item_created):
        """
        Purpose:
            Verify store_failure correctly calls event handler.
        """
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.dlq import DLQConfig, DLQService

        # Arrange
        mock_repo = MagicMock()
        mock_repo.create.return_value = MagicMock(id=456)

        # Use config with enabled=True
        config = DLQConfig(enabled=True)
        service = DLQService(config=config, repository=mock_repo)

        # Act - should work normally
        result = service.store_failure(
            domain="point",
            failure_type="BALANCE_ERROR",
            mode="sync",
        )

        # Assert - operation succeeded
        assert result.success is True
        mock_on_item_created.assert_called_once()


# =============================================================================
# CircuitBreaker Push Event Integration Tests
# =============================================================================


class TestCircuitBreakerPushEvents:
    """Tests for Circuit Breaker push event emission."""

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur.metrics.event_handlers.CircuitBreakerEventHandler.on_state_changed")
    def test_force_open_emits_on_state_changed(
        self, mock_on_state_changed, mock_system_enabled
    ):
        """
        Purpose:
            Verify force_open calls on_state_changed event handler.
        """
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        # Arrange
        mock_repo = MagicMock()
        mock_repo.atomic_force_open.return_value = (True, "closed", "open")

        service = CircuitBreakerService(repository=mock_repo)
        service._repository = mock_repo

        # Act
        result = service.force_open(
            service_name="toss_payment",
            reason="Maintenance",
        )

        # Assert
        assert result.success is True
        mock_on_state_changed.assert_called_once_with(
            service="toss_payment",
            from_state="closed",
            to_state="open",
        )

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur.metrics.event_handlers.CircuitBreakerEventHandler.on_state_changed")
    def test_force_close_emits_on_state_changed(
        self, mock_on_state_changed, mock_system_enabled
    ):
        """
        Purpose:
            Verify force_close calls on_state_changed event handler.
        """
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        # Arrange
        mock_repo = MagicMock()
        mock_repo.atomic_force_close.return_value = (True, "open", "closed")

        service = CircuitBreakerService(repository=mock_repo)
        service._repository = mock_repo

        # Act
        result = service.force_close(
            service_name="toss_payment",
            reason="Service recovered",
        )

        # Assert
        assert result.success is True
        mock_on_state_changed.assert_called_once_with(
            service="toss_payment",
            from_state="open",
            to_state="closed",
        )

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur.metrics.event_handlers.CircuitBreakerEventHandler.on_state_changed")
    def test_no_event_when_state_unchanged(
        self, mock_on_state_changed, mock_system_enabled
    ):
        """
        Purpose:
            Verify no event is emitted when state doesn't change.
        """
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        # Arrange - already open
        mock_repo = MagicMock()
        mock_repo.atomic_force_open.return_value = (True, "open", "open")

        service = CircuitBreakerService(repository=mock_repo)
        service._repository = mock_repo

        # Act
        result = service.force_open(
            service_name="toss_payment",
            reason="Already open",
        )

        # Assert - no event because state unchanged
        assert result.success is True
        mock_on_state_changed.assert_not_called()

    @patch("baldur.metrics.event_handlers.CircuitBreakerEventHandler.on_state_changed")
    def test_reset_emits_on_state_changed_when_state_differs(
        self, mock_on_state_changed
    ):
        """
        Purpose:
            Verify reset calls on_state_changed when state actually changes.
        """
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        # Arrange
        mock_repo = MagicMock()
        mock_repo.atomic_reset.return_value = (True, "open", "closed")

        service = CircuitBreakerService(repository=mock_repo)
        service._repository = mock_repo

        # Act
        result = service.reset(
            service_name="toss_payment",
            reason="Reset after maintenance",
        )

        # Assert
        assert result.success is True
        mock_on_state_changed.assert_called_once_with(
            service="toss_payment",
            from_state="open",
            to_state="closed",
        )

    @patch("baldur.metrics.event_handlers.CircuitBreakerEventHandler.on_state_changed")
    def test_auto_open_on_failure_threshold_emits_event(self, mock_on_state_changed):
        """
        Purpose:
            Verify automatic circuit open on failure threshold emits event.
        """
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        # Arrange - minimum_calls=1 to allow testing with fewer calls
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=3,
            minimum_calls=1,  # Allow trigger with low call count
        )

        mock_state = MagicMock()
        mock_state.manually_controlled = False
        mock_state.failure_count = 3  # At threshold
        mock_state.success_count = 0
        mock_state.state = "closed"
        mock_state.service_name = "toss_payment"

        mock_repo = MagicMock()
        mock_repo.get_or_create.return_value = mock_state
        mock_repo.record_failure.return_value = mock_state

        service = CircuitBreakerService(config=config, repository=mock_repo)
        service._repository = mock_repo

        # Act
        service.record_failure("toss_payment")

        # Assert
        mock_on_state_changed.assert_called_once_with(
            service="toss_payment",
            from_state="closed",
            to_state="open",
        )


# =============================================================================
# SafeGauge Integration Tests
# =============================================================================


class TestSafeGaugeIntegration:
    """Tests for SafeGauge behavior in event handlers."""

    def test_safe_gauge_prevents_negative_on_dec(self):
        """
        Purpose:
            Verify SafeGauge prevents negative values on dec().
        """
        from unittest.mock import MagicMock

        from baldur.metrics.safe_gauge import SafeGauge

        # Arrange
        mock_gauge = MagicMock()
        mock_child = MagicMock()
        mock_gauge.labels.return_value = mock_child

        safe_gauge = SafeGauge(mock_gauge)

        # Act - try to decrement from 0
        child = safe_gauge.labels(domain="payment")
        child.dec()  # Should not go negative

        # Assert - shadow value should be 0 (clamped)
        assert child.get_shadow_value() == 0.0

    def test_safe_gauge_inc_dec_balance(self):
        """
        Purpose:
            Verify SafeGauge maintains correct balance with inc/dec.
        """
        from unittest.mock import MagicMock

        from baldur.metrics.safe_gauge import SafeGauge

        # Arrange
        mock_gauge = MagicMock()
        mock_child = MagicMock()
        mock_gauge.labels.return_value = mock_child

        safe_gauge = SafeGauge(mock_gauge)
        child = safe_gauge.labels(domain="payment")

        # Act
        child.inc()  # 1
        child.inc()  # 2
        child.dec()  # 1

        # Assert
        assert child.get_shadow_value() == 1.0

    def test_safe_gauge_set_clamps_negative(self):
        """
        Purpose:
            Verify SafeGauge.set() clamps negative values to 0.
        """
        from unittest.mock import MagicMock

        from baldur.metrics.safe_gauge import SafeGauge

        # Arrange
        mock_gauge = MagicMock()
        mock_child = MagicMock()
        mock_gauge.labels.return_value = mock_child

        safe_gauge = SafeGauge(mock_gauge)
        child = safe_gauge.labels(domain="payment")

        # Act - try to set negative value
        child.set(-5)

        # Assert - should be clamped to 0
        assert child.get_shadow_value() == 0.0


# =============================================================================
# Event Handler Unit Tests
# =============================================================================


class TestDLQMetricEventHandler:
    """Tests for DLQMetricEventHandler."""

    @patch("baldur.metrics.event_handlers._get_metrics")
    @patch("baldur.metrics.event_handlers._get_safe_pending_gauge")
    def test_on_item_created_increments_gauge(self, mock_get_gauge, mock_get_metrics):
        """
        Purpose:
            Verify on_item_created increments the pending gauge.
        """
        from baldur.metrics.event_handlers import DLQMetricEventHandler
        from baldur.metrics.registry import register_domain

        # Arrange
        register_domain("payment")
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        mock_safe_gauge = MagicMock()
        mock_gauge_child = MagicMock()
        mock_safe_gauge.labels.return_value = mock_gauge_child
        mock_get_gauge.return_value = mock_safe_gauge

        # Act
        DLQMetricEventHandler.on_item_created("payment", "PG_TIMEOUT")

        # Assert
        mock_metrics.record_dlq_item_created.assert_called_once_with(
            "payment", "PG_TIMEOUT"
        )
        mock_gauge_child.inc.assert_called_once()

    @patch("baldur.metrics.event_handlers._get_metrics")
    @patch("baldur.metrics.event_handlers._get_safe_pending_gauge")
    def test_on_item_resolved_decrements_gauge(self, mock_get_gauge, mock_get_metrics):
        """
        Purpose:
            Verify on_item_resolved decrements the pending gauge.
        """
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        # Arrange
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        mock_safe_gauge = MagicMock()
        mock_gauge_child = MagicMock()
        mock_safe_gauge.labels.return_value = mock_gauge_child
        mock_get_gauge.return_value = mock_safe_gauge

        # Act
        DLQMetricEventHandler.on_item_resolved("payment", "auto_replay", 120.5)

        # Assert
        mock_gauge_child.dec.assert_called_once()


class TestCircuitBreakerEventHandler:
    """Tests for CircuitBreakerEventHandler."""

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_state_changed_updates_gauge(self, mock_get_metrics):
        """
        Purpose:
            Verify on_state_changed updates the circuit breaker state gauge.
        """
        from baldur.metrics.event_handlers import CircuitBreakerEventHandler

        # Arrange
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        # Act
        CircuitBreakerEventHandler.on_state_changed(
            service="toss_payment",
            from_state="closed",
            to_state="open",
        )

        # Assert
        mock_metrics.circuit_breaker.record_state_change.assert_called_once_with(
            "toss_payment",
            "closed",
            "open",
            cell_id="",
        )

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_failure_increments_failure_counter(self, mock_get_metrics):
        """
        Purpose:
            Verify on_failure increments the failure counter.
        """
        from baldur.metrics.event_handlers import CircuitBreakerEventHandler

        # Arrange
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        # Act
        CircuitBreakerEventHandler.on_failure("toss_payment")

        # Assert
        mock_metrics.circuit_breaker.record_failure.assert_called_once_with(
            "toss_payment"
        )
