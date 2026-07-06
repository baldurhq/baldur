"""
DailyReportMetricRecorder Unit Tests (394 — R).

Test targets:
    - baldur.metrics.recorders.daily_report.DailyReportMetricRecorder
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, facade registration
    B. Behavior: Method calls, delivery status label mapping

Reference:
    394
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def daily_report_recorder():
    from baldur.metrics.recorders.daily_report import DailyReportMetricRecorder

    return DailyReportMetricRecorder()


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestDailyReportRecorderContract:
    """DailyReportMetricRecorder contract: exports and facade registration."""

    def test_all_exports_exactly_recorder_class(self):
        """__all__ exports recorder class plus DropReason type."""
        from baldur.metrics.recorders.daily_report import __all__

        assert __all__ == ["DailyReportMetricRecorder", "DropReason"]

    def test_facade_has_daily_report_attribute(self):
        """BaldurMetrics exposes daily_report recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.daily_report import DailyReportMetricRecorder

        m = get_metrics()
        assert isinstance(m.daily_report, DailyReportMetricRecorder)


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestDailyReportRecorderBehavior:
    """DailyReportMetricRecorder method behavior."""

    def test_record_generated_does_not_raise(self, daily_report_recorder):
        """record_generated does not raise."""
        daily_report_recorder.record_generated()

    def test_record_delivery_success_passes_success_status(self, daily_report_recorder):
        """record_delivery with success=True passes status='success'."""
        with patch.object(
            daily_report_recorder._delivery_total,
            "labels",
            wraps=daily_report_recorder._delivery_total.labels,
        ) as mock_labels:
            daily_report_recorder.record_delivery("email", success=True)
            call_kwargs = mock_labels.call_args[1]
            assert call_kwargs["status"] == "success"
            assert call_kwargs["channel"] == "email"

    def test_record_delivery_failure_passes_failure_status(self, daily_report_recorder):
        """record_delivery with success=False passes status='failure'."""
        with patch.object(
            daily_report_recorder._delivery_total,
            "labels",
            wraps=daily_report_recorder._delivery_total.labels,
        ) as mock_labels:
            daily_report_recorder.record_delivery("slack", success=False)
            call_kwargs = mock_labels.call_args[1]
            assert call_kwargs["status"] == "failure"
            assert call_kwargs["channel"] == "slack"

    def test_record_skipped_does_not_raise(self, daily_report_recorder):
        """record_skipped with reason does not raise."""
        daily_report_recorder.record_skipped("no_data")

    def test_record_entry_dropped_default_count_passes_one(self, daily_report_recorder):
        """record_entry_dropped without count argument calls .inc(1)."""
        with patch.object(
            daily_report_recorder._entries_dropped_total, "labels"
        ) as mock_labels:
            daily_report_recorder.record_entry_dropped("trimmed")

            mock_labels.assert_called_once_with(reason="trimmed")
            mock_labels.return_value.inc.assert_called_once_with(1)

    def test_record_entry_dropped_explicit_count_forwards_value(
        self, daily_report_recorder
    ):
        """record_entry_dropped(count=N) forwards N to .inc()."""
        with patch.object(
            daily_report_recorder._entries_dropped_total, "labels"
        ) as mock_labels:
            daily_report_recorder.record_entry_dropped("trimmed", count=7)

            mock_labels.return_value.inc.assert_called_once_with(7)

    def test_record_entry_dropped_trimmed_label_passed(self, daily_report_recorder):
        """record_entry_dropped('trimmed') passes reason='trimmed' label."""
        with patch.object(
            daily_report_recorder._entries_dropped_total,
            "labels",
            wraps=daily_report_recorder._entries_dropped_total.labels,
        ) as mock_labels:
            daily_report_recorder.record_entry_dropped("trimmed")

            assert mock_labels.call_args[1]["reason"] == "trimmed"


# =============================================================================
# 484 D1: Daily Report freshness gauge (last_generated_timestamp_seconds)
# =============================================================================


class TestDailyReportLastGeneratedGaugeBehavior:
    """484 D2: ``record_generated()`` sets the freshness gauge to ``time.time()``.

    The gauge enables PromQL ``time() - <gauge> > 1.5d`` alerts for missed
    daily-report runs.
    """

    def test_last_generated_gauge_attribute_exists(self, daily_report_recorder):
        """Recorder exposes ``_last_generated_gauge`` slot (D2 wiring)."""
        assert hasattr(daily_report_recorder, "_last_generated_gauge")
        assert daily_report_recorder._last_generated_gauge is not None

    def test_record_generated_sets_gauge_to_current_time(self, daily_report_recorder):
        """``record_generated()`` calls gauge.set(time.time())."""
        with (
            patch(
                "baldur.metrics.recorders.daily_report.time.time",
                return_value=1_700_000_000.0,
            ),
            patch.object(
                daily_report_recorder._last_generated_gauge, "set"
            ) as mock_set,
        ):
            daily_report_recorder.record_generated()

            mock_set.assert_called_once_with(1_700_000_000.0)

    def test_record_generated_set_uses_real_module_time_source(
        self, daily_report_recorder
    ):
        """``record_generated()`` reads from ``time.time`` in the daily_report module.

        Patching ``baldur.metrics.recorders.daily_report.time.time`` must be
        observed; this guards against accidental migration to a different
        time source (e.g., ``datetime.now()``) without updating call sites.
        """
        sentinel = 4_242_424_242.0
        with (
            patch(
                "baldur.metrics.recorders.daily_report.time.time", return_value=sentinel
            ),
            patch.object(
                daily_report_recorder._last_generated_gauge, "set"
            ) as mock_set,
        ):
            daily_report_recorder.record_generated()

            assert mock_set.call_args.args == (sentinel,)

    def test_record_generated_swallows_exceptions(self, daily_report_recorder):
        """Gauge errors must not crash the daily-report success path."""
        with patch.object(
            daily_report_recorder._last_generated_gauge,
            "set",
            side_effect=RuntimeError("metrics down"),
        ):
            # Should not raise — the recorder swallows metric failures.
            daily_report_recorder.record_generated()
