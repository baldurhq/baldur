"""Tests for the cleanup lane's daily-report recording helper.

Target: ``baldur.services.daily_report.aggregator.record_cleanup_result`` and
the ``AUTO_PROCESSING_INGEST_KEYS`` tuple it gates on.

The digest's Auto-Processing section aggregates counters that only reach it
when a producer explicitly pushes into the collector. The helper is the single
place the cleanup lane's push policy lives, so its three gates are what decide
whether a cleanup run shows up in the digest at all:

1. success — a tier-gated task returns a failure result on every run where its
   backing service is absent, and pushing that would bump ``task_failures``
   daily rather than reporting cleanup.
2. not a dry run — a purge dry-run reports a would-purge count that never
   happened.
3. at least one Auto-Processing counter above zero — an idle run adds nothing.

Covers:
- TestAutoProcessingIngestKeysContract: the key tuple and its alignment with
  the report's own field mapping.
- TestRecordCleanupResultBehavior: the three gates as a result-shape matrix,
  the count boundary, argument forwarding, and the fail-open posture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.services.daily_report.aggregator import (
    AUTO_PROCESSING_INGEST_KEYS,
    DailyReportCollector,
    record_cleanup_result,
)
from baldur.services.daily_report.models import (
    DailyAutonomousReport,
    TaskResultEntry,
)

TASK_NAME = "baldur.archive_old_dlq_entries"


def _patched_collector():
    """Patch the collector singleton `record_cleanup_result` resolves.

    The helper calls `get_daily_report_collector()` as a module global at call
    time, so patching the aggregator attribute intercepts it.
    """
    collector = MagicMock(spec=DailyReportCollector)
    return collector, patch(
        "baldur.services.daily_report.aggregator.get_daily_report_collector",
        return_value=collector,
    )


def _record(result: dict, task_name: str = TASK_NAME) -> MagicMock:
    """Run the helper against a patched collector and return the collector."""
    collector, patcher = _patched_collector()
    with patcher:
        record_cleanup_result(task_name, result)
    return collector


# =============================================================================
# Ingest keys — Contract
# =============================================================================


class TestAutoProcessingIngestKeysContract:
    """The gate keys are the report's Auto-Processing counters, exactly.

    A key the helper gates on but the report does not aggregate would let a
    result through that changes no counter; a counter the report aggregates
    but the helper omits would silently drop that cleanup lane from the
    digest. Both directions are pinned here.
    """

    def test_ingest_keys_are_the_three_auto_processing_counters(self):
        """The design contract: archived / expired / purged."""
        assert AUTO_PROCESSING_INGEST_KEYS == (
            "archived_count",
            "expired_count",
            "purged_count",
        )

    @pytest.mark.parametrize("key", AUTO_PROCESSING_INGEST_KEYS)
    def test_every_ingest_key_increments_its_report_counter(self, key):
        """Each gated key is consumed by the report's field mapping.

        Drives the aggregation the helper feeds, so a key renamed on either
        side breaks here rather than silently rendering 0 in the digest.
        """
        # Given
        report = DailyAutonomousReport()
        entry = TaskResultEntry(
            task_name=TASK_NAME,
            result={key: 7},
            timestamp=datetime(2026, 7, 19, tzinfo=UTC),
        )

        # When
        report.add_entry(entry)

        # Then
        assert getattr(report, key) == 7


# =============================================================================
# record_cleanup_result — Behavior
# =============================================================================


class TestRecordCleanupResultBehavior:
    """The three recording gates, argument forwarding, and fail-open."""

    # --- gate matrix -------------------------------------------------------

    @pytest.mark.parametrize(
        ("result", "should_record"),
        [
            pytest.param(
                {"success": True, "archived_count": 3},
                True,
                id="success_bool_records",
            ),
            pytest.param(
                {"status": "success", "expired_count": 3},
                True,
                id="status_string_records",
            ),
            pytest.param(
                {"success": False, "archived_count": 3, "error": "no dlq service"},
                False,
                id="failure_shape_silent",
            ),
            pytest.param(
                {"status": "error", "expired_count": 3},
                False,
                id="error_status_silent",
            ),
            pytest.param(
                {"success": True, "purged_count": 3, "dry_run": True},
                False,
                id="dry_run_silent",
            ),
            pytest.param(
                {"success": True, "archived_count": 0, "expired_count": 0},
                False,
                id="zero_counts_silent",
            ),
            pytest.param(
                {"success": True, "operation": "archived"},
                False,
                id="no_ingest_key_silent",
            ),
            pytest.param(
                {"success": True, "skipped": True, "reason": "no_statistics_adapter"},
                False,
                id="adapter_skipped_shape_silent",
            ),
            pytest.param(
                {"success": True, "deleted_count": 9},
                False,
                id="non_ingest_counter_silent",
            ),
        ],
    )
    def test_records_only_successful_non_dry_run_results_with_work_done(
        self, result, should_record
    ):
        """The result-shape matrix decides whether the digest sees the run.

        The failure shapes matter most: `archive_old_dlq_entries` and
        `purge_archived_dlq_entries` return `success=False` on every OSS run
        (no PRO DLQ service), and the adapter cleanup task returns the
        `skipped` shape with no statistics adapter. Recording any of them
        would report a daily failure instead of cleanup.
        """
        collector = _record(result)

        assert collector.add_result.called is should_record

    @pytest.mark.parametrize("key", AUTO_PROCESSING_INGEST_KEYS)
    def test_any_single_ingest_counter_above_zero_records(self, key):
        """The gate is an any-of, not an all-of.

        Each in-scope task emits only its own counter, so requiring more than
        one present would drop every lane.
        """
        collector = _record({"success": True, key: 1})

        collector.add_result.assert_called_once()

    @pytest.mark.parametrize(
        ("count", "should_record"),
        [
            pytest.param(0, False, id="zero_is_no_work"),
            pytest.param(1, True, id="one_is_work"),
        ],
    )
    def test_count_boundary_decides_recording(self, count, should_record):
        """The `> 0` boundary — a run that cleaned nothing records nothing."""
        collector = _record({"success": True, "archived_count": count})

        assert collector.add_result.called is should_record

    def test_dry_run_excluded_even_with_a_real_would_purge_count(self):
        """A dry-run purge reports deletions that never happened.

        `CleanupService.purge_archived_dlq_entries(dry_run=True)` returns a
        success result whose `purged_count` is the would-purge count, so the
        success and count gates both pass — only the dry-run gate stops it.
        """
        collector = _record(
            {
                "success": True,
                "operation": "purged",
                "purged_count": 42,
                "dry_run": True,
                "older_than_days": 90,
            }
        )

        collector.add_result.assert_not_called()

    def test_non_dry_run_purge_is_recorded(self):
        """The real purge shape differs from the dry run only by the flag."""
        collector = _record(
            {
                "success": True,
                "operation": "purged",
                "purged_count": 42,
                "dry_run": False,
                "older_than_days": 90,
            }
        )

        collector.add_result.assert_called_once()

    # --- forwarding --------------------------------------------------------

    def test_records_task_name_and_result_verbatim(self):
        """The task name and the whole result dict reach the collector.

        Extra keys are ingest-inert, so the result is pushed unmodified
        rather than filtered down to the counters.
        """
        # Given
        result = {
            "success": True,
            "operation": "archived",
            "archived_count": 5,
            "older_than_days": 30,
        }

        # When
        collector = _record(result, task_name="baldur.cleanup_expired_config")

        # Then
        collector.add_result.assert_called_once_with(
            task_name="baldur.cleanup_expired_config",
            result=result,
        )

    def test_result_dict_is_not_mutated(self):
        """Recording is a read-only side effect on the caller's result.

        The task bodies return the same dict they pass here, so a mutation
        would change what the Celery caller sees.
        """
        result = {"success": True, "archived_count": 5}
        original = dict(result)

        _record(result)

        assert result == original

    # --- fail-open ---------------------------------------------------------

    def test_collector_failure_does_not_propagate_to_the_cleanup_lane(self):
        """A collector failure never fails the cleanup task that reported it."""
        collector = MagicMock(spec=DailyReportCollector)
        collector.add_result.side_effect = RuntimeError("collector down")

        with patch(
            "baldur.services.daily_report.aggregator.get_daily_report_collector",
            return_value=collector,
        ):
            record_cleanup_result(TASK_NAME, {"success": True, "archived_count": 1})

        collector.add_result.assert_called_once()

    def test_collector_failure_logs_warning_with_task_name(self):
        """The swallowed failure still surfaces at WARNING with its context."""
        collector = MagicMock(spec=DailyReportCollector)
        collector.add_result.side_effect = RuntimeError("collector down")

        with (
            patch(
                "baldur.services.daily_report.aggregator.get_daily_report_collector",
                return_value=collector,
            ),
            capture_logs() as logs,
        ):
            record_cleanup_result(TASK_NAME, {"success": True, "archived_count": 1})

        record = next(
            log
            for log in logs
            if log["event"] == "daily_report_collector.record_cleanup_result_failed"
        )
        assert record["log_level"] == "warning"
        assert record["task_name"] == TASK_NAME
        assert "collector down" in str(record["error"])

    def test_collector_resolution_failure_is_swallowed(self):
        """A collector that cannot even be constructed is fail-open too."""
        with (
            patch(
                "baldur.services.daily_report.aggregator.get_daily_report_collector",
                side_effect=RuntimeError("cache unavailable"),
            ),
            capture_logs() as logs,
        ):
            record_cleanup_result(TASK_NAME, {"success": True, "archived_count": 1})

        assert any(
            log["event"] == "daily_report_collector.record_cleanup_result_failed"
            for log in logs
        )

    def test_non_numeric_count_is_swallowed_rather_than_raised(self):
        """A malformed counter degrades to no record, not a task crash.

        The gate coerces with `int(...)`, so a repository returning a
        non-numeric count would raise inside the helper — the cleanup task
        must survive it.
        """
        collector = _record({"success": True, "archived_count": "many"})

        collector.add_result.assert_not_called()
