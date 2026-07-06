"""
Unit Tests for Baldur Observability Metrics

Tests for Prometheus metrics recording, gauge updates, and metric collection.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from baldur.services import (
    collect_all_metrics,
    record_sla_breach,
)
from baldur.services.metrics import (
    get_registered_domains,
    register_domain,
)
from baldur.services.metrics.recorders import (
    record_circuit_breaker_open_duration,
    record_circuit_breaker_state_change,
    record_dlq_item_created,
    record_recovery_time,
    record_replay_attempt,
    record_retry_attempt,
)
from baldur.services.metrics.updaters import (
    track_recovery_time,
)

# =============================================================================
# Domain Registration Tests
# =============================================================================


class TestDomainsConstant:
    """Tests for domain registration functionality."""

    def test_get_registered_domains_returns_list(self):
        """
        Purpose:
            Verify get_registered_domains returns a list type.
        """
        domains = get_registered_domains()
        assert isinstance(domains, list)

    def test_get_registered_domains_contains_defaults(self):
        """
        Purpose:
            Verify get_registered_domains contains default domain values.
        """
        domains = get_registered_domains()
        # Default domains from baldur package
        default_domains = {
            "external_service",
            "internal_process",
            "async_task",
            "notification",
            "data_sync",
        }
        assert default_domains.issubset(set(domains))

    def test_register_domain_adds_new_domain(self):
        """
        Purpose:
            Verify register_domain adds custom domains.
        """
        register_domain("test_domain_xyz")
        domains = get_registered_domains()
        assert "test_domain_xyz" in domains


# =============================================================================
# Metric Recording Unit Tests
# =============================================================================


class TestRecordDLQItemCreated:
    """Tests for DLQ item creation metric recording."""

    def test_record_dlq_item_created_success(self):
        """
        Purpose:
            Verify DLQ item creation metric is recorded without error.
        """
        # Act - should not raise any exception
        record_dlq_item_created(domain="payment", failure_type="PG_TIMEOUT")
        record_dlq_item_created(domain="point", failure_type="NEGATIVE_BALANCE")

    def test_record_dlq_item_created_with_all_domains(self):
        """
        Purpose:
            Verify all domain types from registered domains can be recorded.
        """
        for domain in get_registered_domains():
            record_dlq_item_created(domain=domain, failure_type="TEST_FAILURE")


class TestRecordRetryAttempt:
    """Tests for retry attempt metric recording."""

    def test_record_retry_attempt_success(self):
        """
        Purpose:
            Verify successful retry attempt is recorded.
        """
        record_retry_attempt(domain="payment", attempt_count=1, outcome="success")

    def test_record_retry_attempt_failure(self):
        """
        Purpose:
            Verify failed retry attempt is recorded.
        """
        record_retry_attempt(domain="payment", attempt_count=3, outcome="failure")

    def test_record_retry_attempt_exhausted(self):
        """
        Purpose:
            Verify exhausted retry is recorded.
        """
        record_retry_attempt(domain="webhook", attempt_count=5, outcome="exhausted")


class TestRecordRecoveryTime:
    """Tests for recovery time metric recording."""

    def test_record_recovery_time_success(self):
        """
        Purpose:
            Verify recovery time is calculated and recorded correctly.
        """
        created = datetime(2025, 12, 8, 10, 0, 0, tzinfo=UTC)
        resolved = datetime(2025, 12, 8, 10, 30, 0, tzinfo=UTC)

        # Act - should not raise
        record_recovery_time(
            domain="payment",
            resolution_type="auto_replay",
            created_at=created,
            resolved_at=resolved,
        )

    def test_record_recovery_time_various_durations(self):
        """
        Purpose:
            Verify various recovery durations are recorded.
        """
        base_time = datetime(2025, 12, 8, 10, 0, 0, tzinfo=UTC)
        durations = [
            timedelta(minutes=5),
            timedelta(minutes=30),
            timedelta(hours=1),
            timedelta(hours=4),
        ]

        for delta in durations:
            record_recovery_time(
                domain="point",
                resolution_type="manual_fix",
                created_at=base_time,
                resolved_at=base_time + delta,
            )


class TestRecordSLABreach:
    """Tests for SLA breach metric recording."""

    def test_record_sla_breach_all_domains(self):
        """
        Purpose:
            Verify SLA breach is recorded for each registered domain.
        """
        for domain in get_registered_domains():
            record_sla_breach(domain=domain)


class TestRecordCircuitBreakerMetrics:
    """Tests for circuit breaker metric recording."""

    def test_record_state_change_open(self):
        """
        Purpose:
            Verify circuit breaker state change to OPEN is recorded.
        """
        record_circuit_breaker_state_change(
            service="toss_payment",
            from_state="closed",
            to_state="open",
        )

    def test_record_state_change_half_open(self):
        """
        Purpose:
            Verify circuit breaker state change to HALF_OPEN is recorded.
        """
        record_circuit_breaker_state_change(
            service="toss_payment",
            from_state="open",
            to_state="half_open",
        )

    def test_record_state_change_close(self):
        """
        Purpose:
            Verify circuit breaker state change to CLOSED is recorded.
        """
        record_circuit_breaker_state_change(
            service="toss_payment",
            from_state="half_open",
            to_state="closed",
        )

    def test_record_open_duration(self):
        """
        Purpose:
            Verify circuit breaker open duration is recorded.
        """
        record_circuit_breaker_open_duration(
            service="toss_payment",
            duration_seconds=300.5,
        )


class TestRecordReplayAttempt:
    """Tests for replay attempt metric recording."""

    def test_record_replay_single_success(self):
        """
        Purpose:
            Verify single replay success is recorded.
        """
        record_replay_attempt(domain="payment", replay_type="single", success=True)

    def test_record_replay_batch_failure(self):
        """
        Purpose:
            Verify batch replay failure is recorded.
        """
        record_replay_attempt(domain="point", replay_type="batch", success=False)

    def test_record_replay_conditional(self):
        """
        Purpose:
            Verify conditional replay is recorded.
        """
        record_replay_attempt(domain="webhook", replay_type="conditional", success=True)


# =============================================================================
# Context Manager Tests
# =============================================================================


class TestTrackRecoveryTimeContextManager:
    """Tests for recovery time tracking context manager."""

    def test_context_manager_records_duration(self):
        """
        Purpose:
            Verify context manager records operation duration.
        """
        import time

        with track_recovery_time("payment", "auto_replay"):
            time.sleep(0.01)  # 10ms

    def test_context_manager_records_on_exception(self):
        """
        Purpose:
            Verify duration is recorded even when exception occurs.
        """
        with pytest.raises(ValueError):
            with track_recovery_time("payment", "auto_replay"):
                raise ValueError("Test error")


# =============================================================================
# Collect All Metrics Tests
# =============================================================================


class TestCollectAllMetrics:
    """Tests for collect_all_metrics function."""

    @patch("baldur.services.metrics.updaters.update_retry_success_rates")
    @patch("baldur.services.metrics.updaters.update_circuit_breaker_gauges")
    @patch("baldur.services.metrics.updaters.update_dlq_status_gauges")
    @patch("baldur.services.metrics.updaters.update_dlq_pending_gauges")
    def test_collect_all_metrics_aggregates_results(
        self,
        mock_pending,
        mock_status,
        mock_cb,
        mock_success,
    ):
        """
        Purpose:
            Verify all metrics are collected and aggregated.
        """
        mock_pending.return_value = {"payment": 5}
        mock_status.return_value = {"pending": 10}
        mock_cb.return_value = {"toss_payment": "closed"}
        mock_success.return_value = {"payment": 95.0}

        result = collect_all_metrics()

        assert "dlq_pending_by_domain" in result
        assert "dlq_by_status" in result
        assert "circuit_breaker_states" in result
        assert "retry_success_rates" in result
        assert "collected_at" in result

        assert result["dlq_pending_by_domain"]["payment"] == 5
        assert result["circuit_breaker_states"]["toss_payment"] == "closed"
