"""
Tests for Metric Event Handlers.

Note: Tests use "external_service" (a registered domain) to verify metrics
are called with resolved domain labels. Unregistered domains are resolved
to "OTHER_DOMAIN" by resolve_domain_label().

The handlers route every domain metric through the recorder public methods
(record_state_change / record_trip / record_failure / record_attempt /
record_retry / record_recovery_duration / record_sla_breach / record_started /
record_replay / record_store_duration), so both metrics backends populate the
series identically (645 D1-D4). Assertions inspect those recorder calls.
"""

from unittest.mock import MagicMock, patch

from baldur.metrics.event_handlers import (
    CircuitBreakerEventHandler,
    DLQMetricEventHandler,
    ReplayEventHandler,
)

# Use a registered domain for tests to verify pass-through behavior.
_REGISTERED_DOMAIN = "external_service"


class TestDLQMetricEventHandler:
    """Tests for DLQMetricEventHandler."""

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_item_created_calls_metrics(self, mock_get_metrics):
        """on_item_created should call the appropriate metrics methods."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_item_created(_REGISTERED_DOMAIN, "PG_TIMEOUT")

        mock_metrics.record_dlq_item_created.assert_called_once_with(
            _REGISTERED_DOMAIN, "PG_TIMEOUT"
        )

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_item_created_records_store_duration(self, mock_get_metrics):
        """on_item_created with a duration records the store-duration histogram."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_item_created(
            _REGISTERED_DOMAIN, "PG_TIMEOUT", duration_seconds=0.05
        )

        mock_metrics.dlq.record_store_duration.assert_called_once_with(
            _REGISTERED_DOMAIN, 0.05
        )

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_item_created_handles_missing_metrics(self, mock_get_metrics):
        """on_item_created should handle gracefully when metrics are not available."""
        mock_get_metrics.return_value = None

        # Should not raise
        DLQMetricEventHandler.on_item_created(_REGISTERED_DOMAIN, "PG_TIMEOUT")

    @patch("baldur.metrics.event_handlers._get_safe_pending_gauge")
    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_item_resolved_calls_metrics(
        self, mock_get_metrics, mock_get_safe_gauge
    ):
        """on_item_resolved should update metrics correctly using SafeGauge."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        mock_safe_gauge = MagicMock()
        mock_get_safe_gauge.return_value = mock_safe_gauge

        DLQMetricEventHandler.on_item_resolved(
            domain=_REGISTERED_DOMAIN,
            resolution_type="auto_replay",
            duration_seconds=30.5,
        )

        # SafeGauge dec() via the pending-gauge wrapper.
        mock_safe_gauge.labels.assert_called_with(domain=_REGISTERED_DOMAIN)
        mock_safe_gauge.labels.return_value.dec.assert_called_once()
        # Recovery duration + success outcome via the recorder public methods.
        mock_metrics.retry.record_recovery_duration.assert_called_once_with(
            _REGISTERED_DOMAIN, "auto_replay", 30.5
        )
        mock_metrics.retry.record_retry.assert_called_once_with(
            _REGISTERED_DOMAIN, True
        )

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_item_failed_increments_failure_counter(self, mock_get_metrics):
        """on_item_failed should record a failure attempt."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_item_failed(
            _REGISTERED_DOMAIN, "PG_TIMEOUT", attempt_count=3
        )

        mock_metrics.retry.record_attempt.assert_called_once_with(
            _REGISTERED_DOMAIN, 3, "failure"
        )

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_sla_breach_increments_counter(self, mock_get_metrics):
        """on_sla_breach should increment SLA breach counter."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_sla_breach(_REGISTERED_DOMAIN)

        mock_metrics.retry.record_sla_breach.assert_called_once_with(_REGISTERED_DOMAIN)


class TestCircuitBreakerEventHandler:
    """Tests for CircuitBreakerEventHandler."""

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_state_changed_updates_gauge(self, mock_get_metrics):
        """on_state_changed should record the state change and the trip."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        CircuitBreakerEventHandler.on_state_changed(
            service="payment_api",
            from_state="closed",
            to_state="open",
        )

        mock_metrics.circuit_breaker.record_state_change.assert_called_once_with(
            "payment_api", "closed", "open", cell_id=""
        )
        mock_metrics.circuit_breaker.record_trip.assert_called_once_with("payment_api")

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_state_changed_non_open_skips_trip(self, mock_get_metrics):
        """A non-open transition records the state change but no trip."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        CircuitBreakerEventHandler.on_state_changed(
            service="payment_api",
            from_state="open",
            to_state="closed",
        )

        mock_metrics.circuit_breaker.record_state_change.assert_called_once_with(
            "payment_api", "open", "closed", cell_id=""
        )
        mock_metrics.circuit_breaker.record_trip.assert_not_called()

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_failure_increments_counter(self, mock_get_metrics):
        """on_failure should record a failure."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        CircuitBreakerEventHandler.on_failure("payment_api")

        mock_metrics.circuit_breaker.record_failure.assert_called_once_with(
            "payment_api"
        )


class TestReplayEventHandler:
    """Tests for ReplayEventHandler."""

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_replay_started_increments_counter(self, mock_get_metrics):
        """on_replay_started should record the replay start."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        ReplayEventHandler.on_replay_started(_REGISTERED_DOMAIN, "auto")

        mock_metrics.replay.record_started.assert_called_once_with(
            _REGISTERED_DOMAIN, "auto"
        )

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_replay_completed_records_outcome(self, mock_get_metrics):
        """on_replay_completed should record outcome and duration."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        ReplayEventHandler.on_replay_completed(_REGISTERED_DOMAIN, True, 2.5)

        mock_metrics.replay.record_replay.assert_called_once_with(
            _REGISTERED_DOMAIN, "success", 2.5
        )

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_on_replay_completed_failure_outcome(self, mock_get_metrics):
        """A failed replay records the failure outcome."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        ReplayEventHandler.on_replay_completed(_REGISTERED_DOMAIN, False, 1.0)

        mock_metrics.replay.record_replay.assert_called_once_with(
            _REGISTERED_DOMAIN, "failure", 1.0
        )


# =============================================================================
# Behavior Tests — Domain Cardinality Guard (353)
# =============================================================================

_UNREGISTERED_DOMAIN = "never_registered_xyz"


class TestDLQEventHandlerDomainResolveBehavior:
    """Behavior: DLQ event handlers resolve unregistered domains (353 §3.4)."""

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_item_created_resolves_unregistered_domain(self, mock_get_metrics):
        """on_item_created with unregistered domain uses OTHER_DOMAIN."""
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_item_created(_UNREGISTERED_DOMAIN, "PG_TIMEOUT")

        mock_metrics.record_dlq_item_created.assert_called_once_with(
            _FALLBACK_DOMAIN, "PG_TIMEOUT"
        )

    @patch("baldur.metrics.event_handlers._get_safe_pending_gauge", autospec=True)
    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_item_resolved_resolves_unregistered_domain(
        self, mock_get_metrics, mock_get_safe_gauge
    ):
        """on_item_resolved uses OTHER_DOMAIN for unregistered safe_gauge."""
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        mock_safe_gauge = MagicMock()
        mock_get_safe_gauge.return_value = mock_safe_gauge

        DLQMetricEventHandler.on_item_resolved(
            domain=_UNREGISTERED_DOMAIN,
            resolution_type="auto_replay",
            duration_seconds=10.0,
        )

        mock_safe_gauge.labels.assert_called_with(domain=_FALLBACK_DOMAIN)

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_item_failed_resolves_unregistered_domain(self, mock_get_metrics):
        """on_item_failed with unregistered domain uses OTHER_DOMAIN."""
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_item_failed(
            _UNREGISTERED_DOMAIN, "ERR", attempt_count=2
        )

        assert mock_metrics.retry.record_attempt.call_args.args[0] == _FALLBACK_DOMAIN

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_overflow_rejected_resolves_unregistered_domain(self, mock_get_metrics):
        """on_overflow_rejected with unregistered domain uses OTHER_DOMAIN."""
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        mock_metrics = MagicMock()
        mock_metrics.record_dlq_rejected = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_overflow_rejected(_UNREGISTERED_DOMAIN)

        mock_metrics.record_dlq_rejected.assert_called_once_with(_FALLBACK_DOMAIN)

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_sla_breach_resolves_unregistered_domain(self, mock_get_metrics):
        """on_sla_breach with unregistered domain uses OTHER_DOMAIN."""
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_sla_breach(_UNREGISTERED_DOMAIN)

        mock_metrics.retry.record_sla_breach.assert_called_once_with(_FALLBACK_DOMAIN)


class TestReplayEventHandlerDomainResolveBehavior:
    """Behavior: Replay event handlers resolve unregistered domains (353 §3.4)."""

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_replay_started_resolves_unregistered_domain(self, mock_get_metrics):
        """on_replay_started with unregistered domain uses OTHER_DOMAIN."""
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        ReplayEventHandler.on_replay_started(_UNREGISTERED_DOMAIN, "auto")

        mock_metrics.replay.record_started.assert_called_once_with(
            _FALLBACK_DOMAIN, "auto"
        )

    @patch("baldur.metrics.event_handlers._get_metrics", autospec=True)
    def test_on_replay_completed_resolves_unregistered_domain(self, mock_get_metrics):
        """on_replay_completed with unregistered domain uses OTHER_DOMAIN."""
        from baldur.metrics.registry import _FALLBACK_DOMAIN

        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        ReplayEventHandler.on_replay_completed(_UNREGISTERED_DOMAIN, True, 1.5)

        assert mock_metrics.replay.record_replay.call_args.args[0] == _FALLBACK_DOMAIN
