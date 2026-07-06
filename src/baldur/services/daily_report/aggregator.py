"""
Daily Report Aggregation Logic.

Collects and aggregates task results for daily reporting.
Uses atomic list operations (push_limit/list_range) on cache provider.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog

from baldur.settings.daily_report import get_daily_report_settings

from .models import DailyAutonomousReport, TaskResultEntry

logger = structlog.get_logger()

DAILY_REPORT_CACHE_KEY_PREFIX = "baldur:daily_report"


def _get_daily_report_recorder():
    """Lazy accessor for DailyReportMetricRecorder (graceful if metrics unavailable)."""
    try:
        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        if metrics._initialized:
            return metrics.daily_report
    except Exception:
        pass
    return None


class DailyReportCollector:
    """
    Collects and stores task results for daily aggregation.

    Uses cache provider's push_limit() for atomic append + size cap
    and list_range() for retrieval. Non-critical side-effect data —
    failures are fail-open (entry silently dropped).
    """

    def add_result(
        self,
        task_name: str,
        result: dict[str, Any],
        severity: str = "info",
    ) -> None:
        """Add a task result to today's report via atomic push_limit."""
        from baldur.utils.time import utc_now

        now = utc_now()
        entry_dict = {
            "task_name": task_name,
            "result": result,
            "timestamp": now.isoformat(),
            "severity": severity,
        }

        date_key = now.strftime("%Y-%m-%d")
        cache_key = f"{DAILY_REPORT_CACHE_KEY_PREFIX}:{date_key}"

        try:
            from baldur.factory import ProviderRegistry

            cache_provider = ProviderRegistry.get_cache()
            settings = get_daily_report_settings()
            max_len = settings.max_entries_per_day

            pre_trim_len = cache_provider.push_limit(
                cache_key,
                entry_dict,
                max_len=max_len,
                ttl=timedelta(seconds=settings.cache_ttl),
            )

            if pre_trim_len > max_len:
                dropped_count = pre_trim_len - max_len
                logger.warning(
                    "daily_report_collector.entries_trimmed",
                    date_key=date_key,
                    pre_trim_len=pre_trim_len,
                    max_len=max_len,
                    dropped_count=dropped_count,
                )
                recorder = _get_daily_report_recorder()
                if recorder:
                    recorder.record_entry_dropped("trimmed", count=dropped_count)

        except Exception as e:
            # Cache backend failures are surfaced as cache_operation_errors_total
            # by the adapter layer (drift_metrics.py). We only log here for
            # debugging context — no domain-level metric needed.
            logger.warning(
                "daily_report_collector.add_result_failed",
                error=e,
            )

    def get_report(self, date: datetime | None = None) -> DailyAutonomousReport:
        """Get aggregated report for a specific date (default: yesterday)."""
        from baldur.core.timezone import now

        if date is None:
            date = now() - timedelta(days=1)

        date_key = date.strftime("%Y-%m-%d")
        report = DailyAutonomousReport(date=date)

        try:
            from baldur.factory import ProviderRegistry

            cache_provider = ProviderRegistry.get_cache()
            cache_key = f"{DAILY_REPORT_CACHE_KEY_PREFIX}:{date_key}"
            entries = cache_provider.list_range(cache_key, 0, -1)

            for entry_dict in entries:
                entry = TaskResultEntry(
                    task_name=entry_dict["task_name"],
                    result=entry_dict["result"],
                    timestamp=datetime.fromisoformat(entry_dict["timestamp"]),
                    severity=entry_dict.get("severity", "info"),
                )
                report.add_entry(entry)

        except Exception as e:
            # Cache backend failures are surfaced as cache_operation_errors_total
            # by the adapter layer (drift_metrics.py). We only log here for
            # debugging context — no domain-level metric needed.
            logger.warning(
                "daily_report_collector.cache_read_failed",
                error=e,
            )

        return report


from baldur.utils.singleton import make_singleton_factory

(
    get_daily_report_collector,
    configure_daily_report_collector,
    reset_daily_report_collector,
) = make_singleton_factory("daily_report_collector", DailyReportCollector)


def aggregate_daily_results(
    date: datetime | None = None,
) -> DailyAutonomousReport:
    """
    Aggregate cached task results into a daily report.

    This is a convenience function that uses the singleton collector.

    Args:
        date: Report date (default: yesterday)

    Returns:
        DailyAutonomousReport instance
    """
    collector = get_daily_report_collector()
    return collector.get_report(date)


__all__ = [
    "DAILY_REPORT_CACHE_KEY_PREFIX",
    "DailyReportCollector",
    "get_daily_report_collector",
    "configure_daily_report_collector",
    "reset_daily_report_collector",
    "aggregate_daily_results",
]
