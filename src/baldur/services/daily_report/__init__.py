"""
Daily Report Service Package

Provides daily report generation and notification services.

Module Structure:
- models.py: TaskResultEntry, DailyAutonomousReport, summary dataclasses
- aggregator.py: DailyReportCollector, aggregate_daily_results
- formatters.py: format_report_for_slack, format_report_for_pagerduty
- service.py: DailyReportService
"""

from .aggregator import (
    AUTO_PROCESSING_INGEST_KEYS,
    DAILY_REPORT_CACHE_KEY_PREFIX,
    DailyReportCollector,
    aggregate_daily_results,
    get_daily_report_collector,
    record_cleanup_result,
    reset_daily_report_collector,
)
from .formatters import format_report_for_pagerduty, format_report_for_slack
from .models import (
    ChaosReportSummary,
    DailyAutonomousReport,
    DailyReportData,
    ErrorBudgetGateSummary,
    LoadSheddingSummary,
    TaskResultEntry,
)
from .service import (
    DailyReportService,
    ReportResult,
    get_daily_report_service,
    reset_daily_report_service,
)

__all__ = [
    # Models
    "TaskResultEntry",
    "ChaosReportSummary",
    "LoadSheddingSummary",
    "ErrorBudgetGateSummary",
    "DailyAutonomousReport",
    "DailyReportData",
    # Aggregator
    "AUTO_PROCESSING_INGEST_KEYS",
    "DAILY_REPORT_CACHE_KEY_PREFIX",
    "DailyReportCollector",
    "get_daily_report_collector",
    "reset_daily_report_collector",
    "aggregate_daily_results",
    "record_cleanup_result",
    # Formatters
    "format_report_for_slack",
    "format_report_for_pagerduty",
    # Service
    "ReportResult",
    "DailyReportService",
    "get_daily_report_service",
    "reset_daily_report_service",
]
