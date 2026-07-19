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

# Result keys the report's Auto-Processing section aggregates. A cleanup
# result carrying none of them has nothing to contribute to that section.
AUTO_PROCESSING_INGEST_KEYS = ("archived_count", "expired_count", "purged_count")


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
        from baldur.utils.time import utc_now

        if date is None:
            date = utc_now() - timedelta(days=1)

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


def record_cleanup_result(task_name: str, result: dict[str, Any]) -> None:
    """Record a cleanup-lane task result into today's digest.

    The cleanup lane (DLQ archival, config expiry, archived-entry purge) feeds
    the report's Auto-Processing counters. Recording is a lane side-effect, so
    this lives beside the collector rather than inside the cleanup service, and
    is called from the task bodies — which covers both the Celery-beat lane and
    the framework-independent scheduler lane with one call site each.

    Recorded only when all three hold, so the digest counts real work:

    1. The task succeeded. Both result shapes are accepted — ``success: True``
       (CleanupResult) and ``status: "success"`` (the config-changes task).
       Failure shapes must not be pushed: a tier-gated task returns a failure
       result on every run where its backing service is absent, which would
       bump ``task_failures`` daily rather than reporting cleanup.
    2. It was not a dry run. A purge dry-run reports the would-purge count,
       which never happened.
    3. At least one Auto-Processing counter is present and above zero. A run
       that cleaned nothing adds no information to the digest.

    Fail-open: a recording failure never propagates into the cleanup lane.

    Args:
        task_name: Identifier stored on the entry (the registered task name).
        result: The task's result dict, pushed verbatim — keys outside the
            ingest map are ignored by the report's field mapping.
    """
    try:
        succeeded = result.get("success") is True or result.get("status") == "success"
        if not succeeded or result.get("dry_run"):
            return

        if not any(
            int(result.get(key) or 0) > 0 for key in AUTO_PROCESSING_INGEST_KEYS
        ):
            return

        get_daily_report_collector().add_result(task_name=task_name, result=result)

    except Exception as e:
        logger.warning(
            "daily_report_collector.record_cleanup_result_failed",
            task_name=task_name,
            error=e,
        )


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
    "AUTO_PROCESSING_INGEST_KEYS",
    "DAILY_REPORT_CACHE_KEY_PREFIX",
    "DailyReportCollector",
    "get_daily_report_collector",
    "configure_daily_report_collector",
    "reset_daily_report_collector",
    "aggregate_daily_results",
    "record_cleanup_result",
]
