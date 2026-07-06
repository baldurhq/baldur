"""
Unit tests for #415 DailyReportCollector drop metrics instrumentation.

Test targets:
  - add_result(): record_entry_dropped on trim branch (write-side trimming)
  - add_result(): entries_trimmed log carries dropped_count field
  - get_report(): cache_read_failed event rename (D8 LOGGING_STANDARDS fix)

Cache backend failures (push_limit/list_range raising or swallowing) are
tracked by `cache_operation_errors_total{backend, operation}` at the cache
adapter layer (drift_metrics.py) — see tests/unit/adapters/cache/. The
domain-level `backend_error` reason and `cache_read_failures` counter that
were initially proposed for 415 are intentionally absent: they would not
fire under the production cache adapter contract (Redis/Memory adapters
swallow exceptions and return safe defaults).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.metrics.recorders.daily_report import DailyReportMetricRecorder
from baldur.services.daily_report.aggregator import DailyReportCollector
from baldur.settings.daily_report import DailyReportSettings

# =============================================================================
# add_result — Metric Behavior Tests (trim branch only)
# =============================================================================


class TestAddResultMetricBehavior:
    """Behavior: add_result instruments the trim drop metric."""

    def test_trim_path_records_default_count_one(self):
        """Trim by exactly one entry records record_entry_dropped('trimmed', count=1)."""
        # Given — push_limit returns max_len + 1 (1 entry over capacity)
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        settings = DailyReportSettings()
        mock_cache.push_limit.return_value = settings.max_entries_per_day + 1
        mock_recorder = MagicMock(spec=DailyReportMetricRecorder)

        # When — add_result triggers the trim branch
        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "baldur.services.daily_report.aggregator._get_daily_report_recorder",
                return_value=mock_recorder,
            ),
        ):
            collector.add_result(task_name="t", result={})

        # Then — recorder called with default count=1
        mock_recorder.record_entry_dropped.assert_called_once_with("trimmed", count=1)

    def test_trim_path_records_count_from_pre_trim_minus_max(self):
        """Trim records dropped_count = pre_trim_len - max_len arithmetic."""
        # Given — push_limit returns max_len + 5 (5 entries over capacity)
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        settings = DailyReportSettings()
        excess = 5
        mock_cache.push_limit.return_value = settings.max_entries_per_day + excess
        mock_recorder = MagicMock(spec=DailyReportMetricRecorder)

        # When
        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "baldur.services.daily_report.aggregator._get_daily_report_recorder",
                return_value=mock_recorder,
            ),
        ):
            collector.add_result(task_name="t", result={})

        # Then — recorder receives the exact arithmetic difference
        mock_recorder.record_entry_dropped.assert_called_once_with(
            "trimmed", count=excess
        )

    def test_graceful_when_recorder_unavailable(self):
        """add_result trim branch does not raise when recorder is None."""
        # Given — push_limit triggers trim AND recorder is unavailable
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        settings = DailyReportSettings()
        mock_cache.push_limit.return_value = settings.max_entries_per_day + 1

        # When + Then — should not raise
        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "baldur.services.daily_report.aggregator._get_daily_report_recorder",
                return_value=None,
            ),
        ):
            collector.add_result(task_name="t", result={})


# =============================================================================
# add_result — Side Effect (Log) Tests
# =============================================================================


class TestAddResultMetricSideEffectBehavior:
    """Behavior: entries_trimmed log carries the new dropped_count field."""

    def test_entries_trimmed_log_carries_dropped_count_field(self):
        """entries_trimmed warning log includes dropped_count = pre_trim_len - max_len."""
        # Given
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        settings = DailyReportSettings()
        excess = 3
        mock_cache.push_limit.return_value = settings.max_entries_per_day + excess

        # When
        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=mock_cache,
            ),
            patch("baldur.services.daily_report.aggregator.logger") as mock_logger,
        ):
            collector.add_result(task_name="t", result={})

        # Then — log kwargs include dropped_count with the exact arithmetic value
        mock_logger.warning.assert_called_once()
        log_kwargs = mock_logger.warning.call_args[1]
        assert log_kwargs["dropped_count"] == excess


# =============================================================================
# get_report — Log Event Rename Test (D8 LOGGING_STANDARDS fix)
# =============================================================================


class TestGetReportLogEventRename:
    """Behavior: get_report uses the renamed cache_read_failed event."""

    def test_cache_read_failure_log_event_renamed(self):
        """Read-side failure log event is 'cache_read_failed' (D8 LOGGING_STANDARDS rename)."""
        # Given
        collector = DailyReportCollector()

        # When
        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                side_effect=Exception("cache down"),
            ),
            patch("baldur.services.daily_report.aggregator.logger") as mock_logger,
        ):
            collector.get_report()

        # Then — event name explicitly locks the rename
        mock_logger.warning.assert_called_once()
        event_name = mock_logger.warning.call_args[0][0]
        assert event_name == "daily_report_collector.cache_read_failed"
