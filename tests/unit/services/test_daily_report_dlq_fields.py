"""
Unit tests for D8 DLQ fields in DailyAutonomousReport and DLQ->Daily Report bridge.

Test target:
  - baldur.services.daily_report.models.DailyAutonomousReport (6 new DLQ fields)
  - baldur.metrics.event_handlers.DLQMetricEventHandler (daily report bridge)

Scenarios:
1. Contract: All 6 DLQ fields exist with default value 0
2. Behavior: _update_counts_from_entry correctly maps DLQ result fields
3. Behavior: merge() correctly sums DLQ fields from two reports
4. Behavior: on_item_created pushes dlq_new_entries_count to daily report
5. Behavior: on_item_resolved pushes resolution type breakdown
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from baldur.services.daily_report.models import (
    DailyAutonomousReport,
    TaskResultEntry,
)

# =============================================================================
# DailyAutonomousReport DLQ fields — Contract Tests
# =============================================================================


class TestDailyReportDLQFieldsContract:
    """Hardcoded design values for DLQ fields on DailyAutonomousReport."""

    DLQ_FIELDS = [
        "dlq_pending_count",
        "dlq_new_entries_count",
        "dlq_resolved_count",
        "dlq_manual_resolutions",
        "dlq_ttl_expired",
        "dlq_max_retries_exhausted",
    ]

    def test_all_six_dlq_fields_exist_with_default_zero(self):
        """All 6 DLQ fields exist on DailyAutonomousReport with default 0."""
        report = DailyAutonomousReport()

        for field_name in self.DLQ_FIELDS:
            assert hasattr(report, field_name), f"Missing field: {field_name}"
            assert getattr(report, field_name) == 0, (
                f"Field {field_name} default is not 0"
            )

    def test_dlq_fields_are_independent_of_core_counts(self):
        """DLQ fields are separate from core counts (archived, expired, etc.)."""
        report = DailyAutonomousReport()

        # Set DLQ fields
        report.dlq_new_entries_count = 10
        report.dlq_resolved_count = 5

        # Core counts remain unaffected
        assert report.archived_count == 0
        assert report.expired_count == 0
        assert report.recovered_count == 0


# =============================================================================
# DailyAutonomousReport._update_counts_from_entry — Behavior Tests
# =============================================================================


class TestUpdateCountsFromEntryBehavior:
    """Functional tests for _update_counts_from_entry() DLQ field mapping."""

    def test_update_counts_maps_dlq_new_entries_count(self):
        """Entry result with dlq_new_entries_count increments the report field."""
        # Given
        report = DailyAutonomousReport()
        entry = TaskResultEntry(
            task_name="dlq_item_created",
            result={"dlq_new_entries_count": 3},
            timestamp=datetime.now(UTC),
        )

        # When
        report.add_entry(entry)

        # Then
        assert report.dlq_new_entries_count == 3

    def test_update_counts_maps_dlq_resolved_count(self):
        """Entry result with dlq_resolved_count increments the report field."""
        # Given
        report = DailyAutonomousReport()
        entry = TaskResultEntry(
            task_name="dlq_item_resolved",
            result={"dlq_resolved_count": 1},
            timestamp=datetime.now(UTC),
        )

        # When
        report.add_entry(entry)

        # Then
        assert report.dlq_resolved_count == 1

    def test_update_counts_maps_all_resolution_type_fields(self):
        """All resolution-type fields (manual, ttl_expired, max_retries_exhausted) are mapped."""
        # Given
        report = DailyAutonomousReport()

        entries_data = [
            {"dlq_manual_resolutions": 2},
            {"dlq_ttl_expired": 5},
            {"dlq_max_retries_exhausted": 1},
        ]

        # When
        for result_data in entries_data:
            entry = TaskResultEntry(
                task_name="dlq_update",
                result=result_data,
                timestamp=datetime.now(UTC),
            )
            report.add_entry(entry)

        # Then
        assert report.dlq_manual_resolutions == 2
        assert report.dlq_ttl_expired == 5
        assert report.dlq_max_retries_exhausted == 1

    def test_update_counts_accumulates_multiple_entries(self):
        """Multiple entries accumulate their DLQ counts."""
        # Given
        report = DailyAutonomousReport()

        # When
        for _ in range(3):
            entry = TaskResultEntry(
                task_name="dlq_item_created",
                result={"dlq_new_entries_count": 2},
                timestamp=datetime.now(UTC),
            )
            report.add_entry(entry)

        # Then
        assert report.dlq_new_entries_count == 6

    def test_update_counts_maps_dlq_pending_count(self):
        """dlq_pending_count is excluded from field_mapping (gauge -> snapshot path).

        Entry result with dlq_pending_count should NOT increment the report field
        because pending count is a gauge collected via snapshot, not event-driven.
        """
        # Given
        report = DailyAutonomousReport()
        entry = TaskResultEntry(
            task_name="dlq_status_check",
            result={"dlq_pending_count": 42},
            timestamp=datetime.now(UTC),
        )

        # When
        report.add_entry(entry)

        # Then — gauge excluded from field_mapping, stays at default 0
        assert report.dlq_pending_count == 0


# =============================================================================
# DailyAutonomousReport.merge() — Behavior Tests
# =============================================================================


class TestDailyReportMergeBehavior:
    """Functional tests for merge() with DLQ fields."""

    def test_merge_sums_all_dlq_fields(self):
        """merge() correctly sums all 6 DLQ fields from two reports."""
        # Given
        report_a = DailyAutonomousReport()
        report_a.dlq_pending_count = 10
        report_a.dlq_new_entries_count = 5
        report_a.dlq_resolved_count = 3
        report_a.dlq_manual_resolutions = 2
        report_a.dlq_ttl_expired = 1
        report_a.dlq_max_retries_exhausted = 0

        report_b = DailyAutonomousReport()
        report_b.dlq_pending_count = 20
        report_b.dlq_new_entries_count = 8
        report_b.dlq_resolved_count = 6
        report_b.dlq_manual_resolutions = 4
        report_b.dlq_ttl_expired = 2
        report_b.dlq_max_retries_exhausted = 3

        # When
        report_a.merge(report_b)

        # Then — dlq_pending_count uses max() (gauge), others use sum (counter)
        assert report_a.dlq_pending_count == 20
        assert report_a.dlq_new_entries_count == 13
        assert report_a.dlq_resolved_count == 9
        assert report_a.dlq_manual_resolutions == 6
        assert report_a.dlq_ttl_expired == 3
        assert report_a.dlq_max_retries_exhausted == 3

    def test_merge_does_not_affect_core_counts(self):
        """Merging DLQ fields does not alter core report counts."""
        # Given
        report_a = DailyAutonomousReport()
        report_a.archived_count = 10
        report_a.dlq_new_entries_count = 5

        report_b = DailyAutonomousReport()
        report_b.archived_count = 0
        report_b.dlq_new_entries_count = 3

        # When
        report_a.merge(report_b)

        # Then
        assert report_a.archived_count == 10
        assert report_a.dlq_new_entries_count == 8


# =============================================================================
# DLQ → Daily Report bridge — Behavior Tests
# =============================================================================


class TestDLQEventHandlerDailyReportBridgeBehavior:
    """Functional tests for DLQMetricEventHandler → DailyReportCollector bridge."""

    def test_on_item_created_pushes_dlq_new_entries_count(self):
        """on_item_created pushes dlq_new_entries_count=1 to daily report collector."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        # Given
        mock_metrics = MagicMock()
        mock_collector = MagicMock()

        with (
            patch(
                "baldur.metrics.event_handlers._get_metrics",
                return_value=mock_metrics,
            ),
            patch(
                "baldur.metrics.event_handlers.resolve_domain_label",
                side_effect=lambda d: d,
            ),
            patch(
                "baldur.metrics.event_handlers._get_safe_pending_gauge",
                return_value=None,
            ),
            patch(
                "baldur.metrics.event_handlers._log_event",
            ),
            patch(
                "baldur.metrics.event_handlers.get_daily_report_collector",
                return_value=mock_collector,
            ),
        ):
            # When
            DLQMetricEventHandler.on_item_created(
                domain="payment", failure_type="PG_TIMEOUT"
            )

        # Then — 428 Phase 1.3 (D3): result includes domain + failure_type context
        mock_collector.add_result.assert_called_once_with(
            task_name="dlq_item_created",
            result={
                "dlq_new_entries_count": 1,
                "domain": "payment",
                "failure_type": "PG_TIMEOUT",
            },
        )

    def test_on_item_resolved_pushes_manual_resolution_type(self):
        """on_item_resolved with resolution_type='manual' pushes dlq_manual_resolutions."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()
        mock_collector = MagicMock()

        with (
            patch(
                "baldur.metrics.event_handlers._get_metrics",
                return_value=mock_metrics,
            ),
            patch(
                "baldur.metrics.event_handlers.resolve_domain_label",
                side_effect=lambda d: d,
            ),
            patch(
                "baldur.metrics.event_handlers._get_safe_pending_gauge",
                return_value=None,
            ),
            patch(
                "baldur.metrics.event_handlers._log_event",
            ),
            patch(
                "baldur.metrics.event_handlers.get_daily_report_collector",
                return_value=mock_collector,
            ),
        ):
            # When
            DLQMetricEventHandler.on_item_resolved(
                domain="payment",
                resolution_type="manual",
                duration_seconds=None,
            )

        # Then — 428 Phase 1.3 (D3): result includes domain context
        mock_collector.add_result.assert_called_once_with(
            task_name="dlq_item_resolved",
            result={
                "dlq_resolved_count": 1,
                "domain": "payment",
                "dlq_manual_resolutions": 1,
            },
        )

    def test_on_item_resolved_pushes_ttl_expired_resolution_type(self):
        """on_item_resolved with resolution_type='ttl_expired' pushes dlq_ttl_expired."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()
        mock_collector = MagicMock()

        with (
            patch(
                "baldur.metrics.event_handlers._get_metrics",
                return_value=mock_metrics,
            ),
            patch(
                "baldur.metrics.event_handlers.resolve_domain_label",
                side_effect=lambda d: d,
            ),
            patch(
                "baldur.metrics.event_handlers._get_safe_pending_gauge",
                return_value=None,
            ),
            patch(
                "baldur.metrics.event_handlers._log_event",
            ),
            patch(
                "baldur.metrics.event_handlers.get_daily_report_collector",
                return_value=mock_collector,
            ),
        ):
            # When
            DLQMetricEventHandler.on_item_resolved(
                domain="payment",
                resolution_type="ttl_expired",
                duration_seconds=10.5,
            )

        # Then — 428 Phase 1.3 (D3): result includes domain context
        mock_collector.add_result.assert_called_once_with(
            task_name="dlq_item_resolved",
            result={
                "dlq_resolved_count": 1,
                "domain": "payment",
                "dlq_ttl_expired": 1,
            },
        )

    def test_on_item_resolved_pushes_max_retries_exhausted_resolution_type(self):
        """on_item_resolved with resolution_type='max_retries_exhausted' pushes correct field."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()
        mock_collector = MagicMock()

        with (
            patch(
                "baldur.metrics.event_handlers._get_metrics",
                return_value=mock_metrics,
            ),
            patch(
                "baldur.metrics.event_handlers.resolve_domain_label",
                side_effect=lambda d: d,
            ),
            patch(
                "baldur.metrics.event_handlers._get_safe_pending_gauge",
                return_value=None,
            ),
            patch(
                "baldur.metrics.event_handlers._log_event",
            ),
            patch(
                "baldur.metrics.event_handlers.get_daily_report_collector",
                return_value=mock_collector,
            ),
        ):
            # When
            DLQMetricEventHandler.on_item_resolved(
                domain="inventory",
                resolution_type="max_retries_exhausted",
                duration_seconds=None,
            )

        # Then — 428 Phase 1.3 (D3): result includes domain context
        mock_collector.add_result.assert_called_once_with(
            task_name="dlq_item_resolved",
            result={
                "dlq_resolved_count": 1,
                "domain": "inventory",
                "dlq_max_retries_exhausted": 1,
            },
        )

    def test_on_item_resolved_unknown_resolution_type_pushes_only_resolved_count(self):
        """Unknown resolution_type only pushes dlq_resolved_count (no breakdown field)."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()
        mock_collector = MagicMock()

        with (
            patch(
                "baldur.metrics.event_handlers._get_metrics",
                return_value=mock_metrics,
            ),
            patch(
                "baldur.metrics.event_handlers.resolve_domain_label",
                side_effect=lambda d: d,
            ),
            patch(
                "baldur.metrics.event_handlers._get_safe_pending_gauge",
                return_value=None,
            ),
            patch(
                "baldur.metrics.event_handlers._log_event",
            ),
            patch(
                "baldur.metrics.event_handlers.get_daily_report_collector",
                return_value=mock_collector,
            ),
        ):
            # When
            DLQMetricEventHandler.on_item_resolved(
                domain="payment",
                resolution_type="auto_replay",
                duration_seconds=None,
            )

        # Then — 428 Phase 1.3 (D3): result includes domain context,
        # unknown resolution type has no breakdown field.
        mock_collector.add_result.assert_called_once_with(
            task_name="dlq_item_resolved",
            result={"dlq_resolved_count": 1, "domain": "payment"},
        )
