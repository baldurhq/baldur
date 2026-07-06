"""
Tests for D4: CB->Error Budget direct consume_atomic call.

Source: src/baldur/services/circuit_breaker/service.py (_apply_burn_rate_multiplier)
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest


class TestApplyBurnRateMultiplierBehavior:
    """Behavior tests for _apply_burn_rate_multiplier in CircuitBreakerService."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # Patches baldur_pro.services.error_budget.atomic_consumer (PRO-tier).
        pytest.importorskip("baldur_pro")

    def _make_service(self):
        """Create a CircuitBreakerService with mock repository."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        mock_repo = Mock()
        config = CircuitBreakerConfig(
            cb_open_base_consumption_minutes=1.0,
            cb_open_burn_rate_multiplier=10.0,
        )
        return CircuitBreakerService(config=config, repository=mock_repo)

    @patch(
        "baldur.services.circuit_breaker.service.CircuitBreakerService._emit_event",
    )
    @patch(
        "baldur_pro.services.error_budget.atomic_consumer.get_atomic_budget_consumer",
    )
    def test_calls_consume_atomic_with_correct_params(
        self, mock_get_consumer, mock_emit_event
    ):
        """consume_atomic is called with namespace, raw_minutes, multiplier, budget_key."""
        # Given
        service = self._make_service()
        mock_result = Mock()
        mock_result.success = True
        mock_result.consumed_minutes = 10.0
        mock_result.degraded_mode = False
        mock_consumer = Mock()
        mock_consumer.consume_atomic.return_value = mock_result
        mock_get_consumer.return_value = mock_consumer

        # When
        service._apply_burn_rate_multiplier("payment_api")

        # Then
        mock_consumer.consume_atomic.assert_called_once_with(
            namespace="payment_api",
            raw_minutes=1.0,
            multiplier=10.0,
            budget_key="baldur:payment_api:error_budget",
        )

    @patch(
        "baldur.services.circuit_breaker.service.CircuitBreakerService._emit_event",
    )
    @patch(
        "baldur.services.circuit_breaker.service.logger",
    )
    @patch(
        "baldur_pro.services.error_budget.atomic_consumer.get_atomic_budget_consumer",
    )
    def test_logs_result_with_consumed_minutes_and_degraded_mode(
        self, mock_get_consumer, mock_logger, mock_emit_event
    ):
        """Result is logged with consumed_minutes and degraded_mode fields."""
        # Given
        service = self._make_service()
        mock_result = Mock()
        mock_result.success = True
        mock_result.consumed_minutes = 10.0
        mock_result.degraded_mode = True
        mock_consumer = Mock()
        mock_consumer.consume_atomic.return_value = mock_result
        mock_get_consumer.return_value = mock_consumer

        # When
        service._apply_burn_rate_multiplier("order_api")

        # Then
        mock_logger.info.assert_any_call(
            "circuit_breaker.error_budget_consumed",
            success=True,
            consumed_minutes=10.0,
            degraded_mode=True,
            service_name="order_api",
        )

    @patch(
        "baldur_pro.services.error_budget.atomic_consumer.get_atomic_budget_consumer",
        side_effect=ImportError("no module"),
    )
    def test_import_error_caught_gracefully(self, mock_get_consumer):
        """ImportError does not propagate (graceful degradation)."""
        service = self._make_service()

        # When / Then - no exception raised
        service._apply_burn_rate_multiplier("missing_api")


class TestCBOpenBaseConsumptionMinutesContract:
    """Contract test for cb_open_base_consumption_minutes default value."""

    def test_default_is_1_0(self):
        """CircuitBreakerConfig.cb_open_base_consumption_minutes defaults to 1.0."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        config = CircuitBreakerConfig()
        assert config.cb_open_base_consumption_minutes == 1.0
