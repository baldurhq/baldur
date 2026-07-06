"""
Unit tests for #414 DailyReportCollector atomic storage.

Test targets:
  - add_result(): push_limit delegation, trim warning log, fail-open on error
  - get_report(): list_range delegation, entry deserialization, fail-open empty report
  - Singleton: get_daily_report_collector / reset_daily_report_collector
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from baldur.services.daily_report.aggregator import (
    DAILY_REPORT_CACHE_KEY_PREFIX,
    DailyReportCollector,
    get_daily_report_collector,
    reset_daily_report_collector,
)
from baldur.services.daily_report.models import DailyAutonomousReport
from baldur.settings.daily_report import DailyReportSettings

# =============================================================================
# add_result — Behavior Tests
# =============================================================================


class TestAddResultBehavior:
    """Behavior: add_result delegates to push_limit with correct arguments."""

    def test_add_result_calls_push_limit_with_settings_values(self):
        """add_result passes max_entries_per_day and cache_ttl from settings."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 1
        settings = DailyReportSettings()

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            collector.add_result(task_name="test", result={"k": 1})

        mock_cache.push_limit.assert_called_once()
        call_kwargs = mock_cache.push_limit.call_args
        assert call_kwargs[1]["max_len"] == settings.max_entries_per_day
        assert call_kwargs[1]["ttl"] == timedelta(seconds=settings.cache_ttl)

    def test_add_result_builds_correct_entry_dict(self):
        """add_result pushes dict with task_name, result, timestamp, severity."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 1

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            collector.add_result(
                task_name="cb_state_changed",
                result={"service": "payment"},
                severity="warning",
            )

        pushed_value = mock_cache.push_limit.call_args[0][1]
        assert pushed_value["task_name"] == "cb_state_changed"
        assert pushed_value["result"] == {"service": "payment"}
        assert pushed_value["severity"] == "warning"
        assert "timestamp" in pushed_value

    def test_add_result_uses_date_key_in_cache_key(self):
        """add_result constructs cache key as prefix:YYYY-MM-DD."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 1

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            collector.add_result(task_name="t", result={})

        cache_key = mock_cache.push_limit.call_args[0][0]
        assert cache_key.startswith(f"{DAILY_REPORT_CACHE_KEY_PREFIX}:")

    def test_add_result_default_severity_is_info(self):
        """add_result defaults severity to 'info' when not specified."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 1

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            collector.add_result(task_name="t", result={})

        pushed_value = mock_cache.push_limit.call_args[0][1]
        assert pushed_value["severity"] == "info"


# =============================================================================
# add_result — Timestamp Consistency Tests
# =============================================================================


class TestAddResultTimestampConsistencyBehavior:
    """Behavior: add_result uses a single utc_now() for both timestamp and date_key."""

    def test_add_result_timestamp_matches_cache_key_date(self):
        """Entry timestamp date and cache_key date are always consistent."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 1

        # Given — freeze time at a specific moment
        fixed_time = datetime(2026, 4, 5, 15, 30, 0, tzinfo=UTC)

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=mock_cache,
            ),
            patch(
                "baldur.utils.time.utc_now",
                return_value=fixed_time,
            ),
        ):
            collector.add_result(task_name="t", result={})

        # Then — cache key date and entry timestamp date match
        cache_key = mock_cache.push_limit.call_args[0][0]
        entry_dict = mock_cache.push_limit.call_args[0][1]
        entry_ts = datetime.fromisoformat(entry_dict["timestamp"])

        assert cache_key == f"{DAILY_REPORT_CACHE_KEY_PREFIX}:2026-04-05"
        assert entry_ts.strftime("%Y-%m-%d") == "2026-04-05"


# =============================================================================
# add_result — Side Effect Tests (logging)
# =============================================================================


class TestAddResultSideEffectBehavior:
    """Behavior: add_result emits correct log events."""

    def test_add_result_logs_entries_trimmed_when_pre_trim_exceeds_max(self):
        """Warning log emitted when push_limit return > max_len."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        settings = DailyReportSettings()
        mock_cache.push_limit.return_value = settings.max_entries_per_day + 1

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=mock_cache,
            ),
            patch("baldur.services.daily_report.aggregator.logger") as mock_logger,
        ):
            collector.add_result(task_name="t", result={})

        mock_logger.warning.assert_called_once()
        event_name = mock_logger.warning.call_args[0][0]
        assert event_name == "daily_report_collector.entries_trimmed"

    def test_add_result_no_trim_log_when_within_limit(self):
        """No warning log when push_limit return <= max_len."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 10  # well within default 5000

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=mock_cache,
            ),
            patch("baldur.services.daily_report.aggregator.logger") as mock_logger,
        ):
            collector.add_result(task_name="t", result={})

        mock_logger.warning.assert_not_called()

    def test_add_result_logs_add_result_failed_on_exception(self):
        """Warning log with 'add_result_failed' event on cache exception."""
        collector = DailyReportCollector()

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                side_effect=RuntimeError("boom"),
            ),
            patch("baldur.services.daily_report.aggregator.logger") as mock_logger,
        ):
            collector.add_result(task_name="t", result={})

        mock_logger.warning.assert_called_once()
        event_name = mock_logger.warning.call_args[0][0]
        assert event_name == "daily_report_collector.add_result_failed"


# =============================================================================
# add_result — Fail-open Behavior
# =============================================================================


class TestAddResultFailOpenBehavior:
    """Behavior: add_result is fail-open (no exception raised on failure)."""

    def test_add_result_does_not_raise_on_cache_error(self):
        """add_result silently drops entry when cache raises exception."""
        collector = DailyReportCollector()

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            side_effect=Exception("cache unavailable"),
        ):
            # Should NOT raise
            collector.add_result(task_name="t", result={"data": 1})

    def test_add_result_does_not_raise_on_push_limit_error(self):
        """add_result silently drops when push_limit itself raises."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.push_limit.side_effect = Exception("push failed")

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            collector.add_result(task_name="t", result={})


# =============================================================================
# get_report — Behavior Tests
# =============================================================================


class TestGetReportBehavior:
    """Behavior: get_report delegates to list_range and deserializes entries."""

    def test_get_report_calls_list_range_with_full_range(self):
        """get_report calls list_range(key, 0, -1) for all entries."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.list_range.return_value = []
        test_date = datetime(2026, 4, 5, tzinfo=UTC)

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            collector.get_report(date=test_date)

        expected_key = f"{DAILY_REPORT_CACHE_KEY_PREFIX}:2026-04-05"
        mock_cache.list_range.assert_called_once_with(expected_key, 0, -1)

    def test_get_report_deserializes_entries_into_report(self):
        """get_report converts list_range dicts into TaskResultEntry objects."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        ts = datetime(2026, 4, 5, 12, 0, tzinfo=UTC)
        mock_cache.list_range.return_value = [
            {
                "task_name": "cb_state_changed",
                "result": {"from": "closed", "to": "open"},
                "timestamp": ts.isoformat(),
                "severity": "warning",
            },
            {
                "task_name": "dlq_item_created",
                "result": {"queue": "payment"},
                "timestamp": ts.isoformat(),
                "severity": "info",
            },
        ]

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            report = collector.get_report(date=ts)

        assert isinstance(report, DailyAutonomousReport)
        assert len(report.entries) == 2

    def test_get_report_returns_empty_report_on_cache_error(self):
        """get_report returns empty report when cache raises exception."""
        collector = DailyReportCollector()
        test_date = datetime(2026, 4, 5, tzinfo=UTC)

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            side_effect=Exception("cache down"),
        ):
            report = collector.get_report(date=test_date)

        assert isinstance(report, DailyAutonomousReport)
        assert len(report.entries) == 0

    def test_get_report_defaults_severity_to_info(self):
        """get_report defaults severity to 'info' when key is missing."""
        collector = DailyReportCollector()
        mock_cache = MagicMock()
        ts = datetime(2026, 4, 5, 12, 0, tzinfo=UTC)
        mock_cache.list_range.return_value = [
            {
                "task_name": "test",
                "result": {},
                "timestamp": ts.isoformat(),
                # no "severity" key
            },
        ]

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            report = collector.get_report(date=ts)

        assert report.entries[0].severity == "info"


# =============================================================================
# Singleton — Behavior Tests
# =============================================================================


class TestCollectorSingletonBehavior:
    """Behavior: get/reset singleton lifecycle."""

    def test_get_daily_report_collector_returns_same_instance(self):
        """get_daily_report_collector returns cached singleton."""
        reset_daily_report_collector()
        try:
            c1 = get_daily_report_collector()
            c2 = get_daily_report_collector()
            assert c1 is c2
        finally:
            reset_daily_report_collector()

    def test_reset_daily_report_collector_clears_instance(self):
        """reset_daily_report_collector forces new instance on next get."""
        reset_daily_report_collector()
        try:
            c1 = get_daily_report_collector()
            reset_daily_report_collector()
            c2 = get_daily_report_collector()
            assert c1 is not c2
        finally:
            reset_daily_report_collector()


# =============================================================================
# _memory_storage removal — Contract Tests
# =============================================================================


class TestMemoryStorageRemovedContract:
    """Contract: DailyReportCollector no longer has _memory_storage attribute."""

    def test_no_memory_storage_attribute(self):
        """DailyReportCollector instances must not have _memory_storage."""
        collector = DailyReportCollector()
        assert not hasattr(collector, "_memory_storage")

    def test_no_clear_old_data_method(self):
        """DailyReportCollector must not have clear_old_data method."""
        assert not hasattr(DailyReportCollector, "clear_old_data")


# =============================================================================
# DailyReportSettings.max_entries_per_day — Contract Tests
# =============================================================================


class TestMaxEntriesPerDaySettingsContract:
    """Contract: max_entries_per_day field design values from 414 document."""

    def test_max_entries_per_day_default_is_5000(self):
        """Default value is 5000 per 414 scope table."""
        settings = DailyReportSettings()
        assert settings.max_entries_per_day == 5000

    def test_max_entries_per_day_minimum_boundary(self):
        """ge=100: value 99 is rejected, 100 is accepted."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DailyReportSettings(max_entries_per_day=99)
        settings = DailyReportSettings(max_entries_per_day=100)
        assert settings.max_entries_per_day == 100

    def test_max_entries_per_day_maximum_boundary(self):
        """le=50000: value 50000 is accepted, 50001 is rejected."""
        import pytest
        from pydantic import ValidationError

        settings = DailyReportSettings(max_entries_per_day=50000)
        assert settings.max_entries_per_day == 50000
        with pytest.raises(ValidationError):
            DailyReportSettings(max_entries_per_day=50001)
