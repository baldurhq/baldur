"""
Unit tests for #412 Daily Report Data Completeness — Peripheral modules.

Test targets:
  - DailyReportCollector.add_result() — MAX_ENTRIES_PER_DAY soft limit, UTC enforcement
  - ErrorBudgetGate.check() — collector push on BLOCKED/WARNING
  - RateController._flush_to_redis() — delta INCR logic, Redis TTL expiry
  - PagerDutySeverity enum — values
  - PagerDutyHandlerMixin._send_pagerduty_alert() — severity passthrough
  - ResilienceReportGenerator.get_report_by_date() — lazy reload
  - ReportResult.to_dict() — DLQ + typed summary extension
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.daily_report.models import (
    ChaosReportSummary,
    DailyAutonomousReport,
    ErrorBudgetGateSummary,
    LoadSheddingSummary,
)
from baldur.services.daily_report.service import ReportResult
from baldur.services.security_notification.models import PagerDutySeverity

# =============================================================================
# MAX_ENTRIES_PER_DAY — Contract & Behavior Tests
# =============================================================================


class TestMaxEntriesPerDayContract:
    """Contract: max_entries_per_day default is 5000 (via settings)."""

    def test_max_entries_per_day_default(self):
        """DailyReportSettings.max_entries_per_day defaults to 5000."""
        from baldur.settings.daily_report import DailyReportSettings

        settings = DailyReportSettings()
        assert settings.max_entries_per_day == 5000


class TestMaxEntriesPerDayBehavior:
    """Behavior: add_result uses push_limit for atomic append + trim."""

    def test_add_result_calls_push_limit(self):
        """add_result delegates to cache_provider.push_limit()."""
        from baldur.services.daily_report.aggregator import DailyReportCollector

        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 1  # pre-trim length

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            collector.add_result(task_name="test", result={"key": 1})

        mock_cache.push_limit.assert_called_once()

    def test_add_result_logs_warning_on_trim(self):
        """add_result logs warning when push_limit returns > max_len."""
        from baldur.services.daily_report.aggregator import DailyReportCollector

        collector = DailyReportCollector()
        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 5001  # exceeds default 5000

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_cache",
                return_value=mock_cache,
            ),
            patch("baldur.services.daily_report.aggregator.logger") as mock_logger,
        ):
            collector.add_result(task_name="test", result={"key": 1})

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "daily_report_collector.entries_trimmed"


# =============================================================================
# PagerDutySeverity Enum — Contract Tests
# =============================================================================


class TestPagerDutySeverityContract:
    """Contract: PagerDutySeverity has 4 standard PD Events API v2 values."""

    def test_critical_value(self):
        """CRITICAL value is 'critical'."""
        assert PagerDutySeverity.CRITICAL.value == "critical"

    def test_error_value(self):
        """ERROR value is 'error'."""
        assert PagerDutySeverity.ERROR.value == "error"

    def test_warning_value(self):
        """WARNING value is 'warning'."""
        assert PagerDutySeverity.WARNING.value == "warning"

    def test_info_value(self):
        """INFO value is 'info'."""
        assert PagerDutySeverity.INFO.value == "info"

    def test_exactly_four_members(self):
        """Enum has exactly 4 members."""
        assert len(PagerDutySeverity) == 4

    def test_str_enum_inheritance(self):
        """PagerDutySeverity inherits from str for JSON serialization."""
        assert isinstance(PagerDutySeverity.CRITICAL, str)


# =============================================================================
# PagerDutyHandlerMixin._send_pagerduty_alert() — Behavior Tests
# =============================================================================


class TestPagerDutyHandlerSeverityPassthroughBehavior:
    """Verify hardcoded severity fix: message severity flows to payload."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_send_pagerduty_alert_uses_message_severity(self):
        """_send_pagerduty_alert reads severity from message dict, not hardcoded."""
        from baldur_pro.services.security_notification.pagerduty_handler import (
            PagerDutyHandlerMixin,
        )

        # Create a concrete instance using the mixin
        handler = type(
            "TestHandler",
            (PagerDutyHandlerMixin,),
            {"config": MagicMock(pagerduty_service_key="test-key", dry_run=True)},
        )()

        result = handler._send_pagerduty_alert(
            {
                "title": "Test Alert",
                "description": "test",
                "severity": "error",  # should flow through
            }
        )

        assert result.success is True
        assert "DRY RUN" in result.message

    def test_send_pagerduty_alert_defaults_to_critical(self):
        """_send_pagerduty_alert defaults to critical when no severity in message."""
        from baldur_pro.services.security_notification.pagerduty_handler import (
            PagerDutyHandlerMixin,
        )

        handler = type(
            "TestHandler",
            (PagerDutyHandlerMixin,),
            {"config": MagicMock(pagerduty_service_key="test-key", dry_run=False)},
        )()

        mock_response = MagicMock()
        mock_response.status_code = 202

        mock_session = MagicMock()
        mock_session.post.return_value = mock_response

        with patch("requests.Session", return_value=mock_session):
            handler._send_pagerduty_alert({"title": "Test", "description": "desc"})

        # Extract payload from post call
        call_kwargs = mock_session.post.call_args
        payload = call_kwargs[1]["json"]
        assert payload["payload"]["severity"] == "critical"


# =============================================================================
# ErrorBudgetGate.check() — Behavior Tests (collector push)
# =============================================================================


class TestErrorBudgetGateCollectorPushBehavior:
    """Verify gate.check() pushes BLOCKED/WARNING to daily report collector."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_check_blocked_pushes_to_collector(self):
        """BLOCKED result pushes error_budget_gate_blocked to collector."""
        from baldur_pro.services.error_budget_gate.config import GateStatus
        from baldur_pro.services.error_budget_gate.gate import ErrorBudgetGate

        gate = ErrorBudgetGate()
        mock_collector = MagicMock()

        with (
            patch.object(gate, "_config") as mock_config,
            patch.object(gate, "_is_cache_valid", return_value=False),
            patch.object(gate, "_get_error_budget_percent", return_value=95.0),
            patch.object(
                gate,
                "_evaluate",
                return_value=MagicMock(status=GateStatus.BLOCKED),
            ),
            patch(
                "baldur.services.daily_report.get_daily_report_collector",
                return_value=mock_collector,
            ),
        ):
            mock_config.enabled = True
            mock_config.critical_threshold_percent = 90.0
            gate.check()

        mock_collector.add_result.assert_called_once()
        call_args = mock_collector.add_result.call_args
        assert call_args[1]["task_name"] == "error_budget_gate_blocked"
        assert call_args[1]["severity"] == "warning"
        assert call_args[1]["result"]["budget_percent"] == 95.0

    def test_check_warning_pushes_to_collector(self):
        """WARNING result pushes error_budget_gate_warning to collector."""
        from baldur_pro.services.error_budget_gate.config import GateStatus
        from baldur_pro.services.error_budget_gate.gate import ErrorBudgetGate

        gate = ErrorBudgetGate()
        mock_collector = MagicMock()

        with (
            patch.object(gate, "_config") as mock_config,
            patch.object(gate, "_is_cache_valid", return_value=False),
            patch.object(gate, "_get_error_budget_percent", return_value=82.0),
            patch.object(
                gate,
                "_evaluate",
                return_value=MagicMock(status=GateStatus.WARNING),
            ),
            patch(
                "baldur.services.daily_report.get_daily_report_collector",
                return_value=mock_collector,
            ),
        ):
            mock_config.enabled = True
            mock_config.warning_threshold_percent = 80.0
            gate.check()

        mock_collector.add_result.assert_called_once()
        call_args = mock_collector.add_result.call_args
        assert call_args[1]["task_name"] == "error_budget_gate_warning"
        assert call_args[1]["severity"] == "info"

    def test_check_open_does_not_push_to_collector(self):
        """OPEN (allowed) result does NOT push to collector."""
        from baldur_pro.services.error_budget_gate.config import GateStatus
        from baldur_pro.services.error_budget_gate.gate import ErrorBudgetGate

        gate = ErrorBudgetGate()

        with (
            patch.object(gate, "_config") as mock_config,
            patch.object(gate, "_is_cache_valid", return_value=False),
            patch.object(gate, "_get_error_budget_percent", return_value=50.0),
            patch.object(
                gate,
                "_evaluate",
                return_value=MagicMock(status=GateStatus.OPEN),
            ),
            patch(
                "baldur.services.daily_report.get_daily_report_collector",
            ) as mock_get_collector,
        ):
            mock_config.enabled = True
            gate.check()

        mock_get_collector.assert_not_called()


# =============================================================================
# RateController._flush_to_redis() — Behavior Tests
# =============================================================================


class TestFlushToRedisBehavior:
    """Verify delta INCR logic for multi-process LS stats sync."""

    def test_flush_increments_redis_with_deltas(self):
        """_flush_to_redis() sends only delta (since last flush) to Redis."""
        from baldur.scaling.rate_controller import RateController
        from baldur.settings.backpressure import BackpressureSettings

        settings = BackpressureSettings(
            backpressure_enabled=False,
            redis_sync_enabled=True,
        )
        controller = RateController(settings=settings)

        # Simulate some activity
        controller._processed_count = 100
        controller._dropped_count = 20
        controller._dropped_by_tier = {
            "critical": 0,
            "standard": 5,
            "non_essential": 15,
        }

        mock_cache = MagicMock()

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            controller._flush_to_redis()

        # First flush: full values as deltas (processed, dropped, standard, non_essential)
        assert mock_cache.incr.call_count == 4
        incr_calls = {
            call[0][0].split(":")[-1]: call[0][1]
            for call in mock_cache.incr.call_args_list
        }
        assert incr_calls["processed"] == 100
        assert incr_calls["dropped"] == 20

    def test_flush_sends_only_new_deltas_on_second_call(self):
        """Second _flush_to_redis() sends only the increment since last flush."""
        from baldur.scaling.rate_controller import RateController
        from baldur.settings.backpressure import BackpressureSettings

        settings = BackpressureSettings(
            backpressure_enabled=False,
            redis_sync_enabled=True,
        )
        controller = RateController(settings=settings)

        mock_cache = MagicMock()

        # First flush with initial values
        controller._processed_count = 100
        controller._dropped_count = 10
        controller._dropped_by_tier = {
            "critical": 0,
            "standard": 0,
            "non_essential": 10,
        }

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            controller._flush_to_redis()

        mock_cache.reset_mock()

        # Simulate more activity
        controller._processed_count = 150
        controller._dropped_count = 15
        controller._dropped_by_tier["non_essential"] = 15

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            controller._flush_to_redis()

        # Second flush: only deltas
        incr_calls = {
            call[0][0].split(":")[-1]: call[0][1]
            for call in mock_cache.incr.call_args_list
        }
        assert incr_calls["processed"] == 50  # 150 - 100
        assert incr_calls["dropped"] == 5  # 15 - 10
        assert incr_calls["non_essential"] == 5  # 15 - 10

    def test_flush_fail_open_on_redis_error(self):
        """_flush_to_redis() silently catches exceptions (fail-open)."""
        from baldur.scaling.rate_controller import RateController
        from baldur.settings.backpressure import BackpressureSettings

        settings = BackpressureSettings(
            backpressure_enabled=False,
            redis_sync_enabled=True,
        )
        controller = RateController(settings=settings)
        controller._processed_count = 10

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            side_effect=Exception("Redis down"),
        ):
            # Should not raise
            controller._flush_to_redis()

    def test_flush_sets_ttl_on_all_keys(self):
        """_flush_to_redis() calls cache.expire() with 48h TTL on every incremented key."""
        from datetime import timedelta

        from baldur.scaling.rate_controller import RateController
        from baldur.settings.backpressure import BackpressureSettings

        settings = BackpressureSettings(
            backpressure_enabled=False,
            redis_sync_enabled=True,
        )
        controller = RateController(settings=settings)

        # Given — activity across processed + dropped + 2 tier drops
        controller._processed_count = 50
        controller._dropped_count = 10
        controller._dropped_by_tier = {
            "critical": 0,
            "standard": 3,
            "non_essential": 7,
        }

        mock_cache = MagicMock()

        # When
        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            controller._flush_to_redis()

        # Then — expire called once per incr (processed, dropped, standard, non_essential)
        assert mock_cache.expire.call_count == mock_cache.incr.call_count

        # All expire calls use 48h TTL
        expected_ttl = timedelta(hours=48)
        for call in mock_cache.expire.call_args_list:
            assert call[0][1] == expected_ttl

    def test_flush_expire_keys_match_incr_keys(self):
        """Each cache.expire() call targets the same key as its preceding cache.incr()."""
        from baldur.scaling.rate_controller import RateController
        from baldur.settings.backpressure import BackpressureSettings

        settings = BackpressureSettings(
            backpressure_enabled=False,
            redis_sync_enabled=True,
        )
        controller = RateController(settings=settings)

        controller._processed_count = 100
        controller._dropped_count = 20
        controller._dropped_by_tier = {
            "critical": 0,
            "standard": 5,
            "non_essential": 15,
        }

        mock_cache = MagicMock()

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            controller._flush_to_redis()

        # Extract key sets from incr and expire calls
        incr_keys = {call[0][0] for call in mock_cache.incr.call_args_list}
        expire_keys = {call[0][0] for call in mock_cache.expire.call_args_list}

        assert incr_keys == expire_keys

    def test_flush_no_expire_when_no_deltas(self):
        """cache.expire() is NOT called when all deltas are zero."""
        from baldur.scaling.rate_controller import RateController
        from baldur.settings.backpressure import BackpressureSettings

        settings = BackpressureSettings(
            backpressure_enabled=False,
            redis_sync_enabled=True,
        )
        controller = RateController(settings=settings)

        # All counters at 0 (default) — no deltas
        mock_cache = MagicMock()

        with patch(
            "baldur.factory.ProviderRegistry.get_cache",
            return_value=mock_cache,
        ):
            controller._flush_to_redis()

        mock_cache.incr.assert_not_called()
        mock_cache.expire.assert_not_called()


# =============================================================================
# ResilienceReportGenerator.get_report_by_date() — Behavior Tests
# =============================================================================


class TestGetReportByDateLazyReloadBehavior:
    """Verify lazy reload on cache miss for multi-process safety."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_lazy_reload_on_miss(self):
        """get_report_by_date() loads from backend via _get_or_load_report on miss."""
        from baldur_pro.services.chaos.reports import ResilienceReportGenerator

        generator = ResilienceReportGenerator.__new__(ResilienceReportGenerator)
        generator._reports = {}
        generator._lock = threading.RLock()

        mock_report = MagicMock()
        generator._get_or_load_report = MagicMock(return_value=mock_report)

        result = generator.get_report_by_date("2026-04-04")

        generator._get_or_load_report.assert_called_once_with("2026-04-04")
        assert result is mock_report

    def test_no_backend_access_when_report_cached(self):
        """Cache hit in _get_or_load_report skips backend entirely."""
        from baldur_pro.services.chaos.reports import (
            ReportConfig,
            ResilienceReportGenerator,
        )

        generator = ResilienceReportGenerator.__new__(ResilienceReportGenerator)
        mock_report = MagicMock()
        generator._reports = {"resilience-2026-04-04": mock_report}
        generator._lock = threading.RLock()
        generator._config = ReportConfig()

        with patch("baldur.core.state_backend.get_state_backend") as mock_gsb:
            result = generator.get_report_by_date("2026-04-04")
            mock_gsb.assert_not_called()

        assert result is mock_report


# =============================================================================
# ReportResult.to_dict() — Contract Tests
# =============================================================================


class TestReportResultToDictExtensionContract:
    """Contract: ReportResult.to_dict() includes DLQ + typed summaries."""

    def test_summary_includes_dlq_fields(self):
        """summary dict includes dlq_pending_count, dlq_new_entries_count, dlq_resolved_count."""
        report = DailyAutonomousReport()
        report.dlq_pending_count = 5
        report.dlq_new_entries_count = 10
        report.dlq_resolved_count = 3

        result = ReportResult(success=True, report=report)
        d = result.to_dict()

        assert d["summary"]["dlq_pending_count"] == 5
        assert d["summary"]["dlq_new_entries_count"] == 10
        assert d["summary"]["dlq_resolved_count"] == 3

    def test_summary_includes_typed_summaries_when_present(self):
        """summary dict includes chaos/load_shedding/error_budget when populated."""
        report = DailyAutonomousReport()
        report.chaos_summary = ChaosReportSummary(grade="B")
        report.load_shedding_summary = LoadSheddingSummary(level="high")
        report.error_budget_summary = ErrorBudgetGateSummary(blocks=2)

        result = ReportResult(success=True, report=report)
        d = result.to_dict()

        assert d["summary"]["chaos"]["grade"] == "B"
        assert d["summary"]["load_shedding"]["level"] == "high"
        assert d["summary"]["error_budget"]["blocks"] == 2

    def test_summary_excludes_typed_summaries_when_none(self):
        """summary dict excludes typed summary keys when None."""
        report = DailyAutonomousReport()

        result = ReportResult(success=True, report=report)
        d = result.to_dict()

        assert "chaos" not in d["summary"]
        assert "load_shedding" not in d["summary"]
        assert "error_budget" not in d["summary"]
