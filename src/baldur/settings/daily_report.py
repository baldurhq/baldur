"""
Daily Report Settings - Pydantic v2.

Settings for the daily autonomous operation report generation task.

Source:
- tasks/daily_report.py

Environment Variables:
    BALDUR_DAILY_REPORT_MAX_RETRIES=2
    BALDUR_DAILY_REPORT_RETRY_DELAY=300
    BALDUR_DAILY_REPORT_DEFAULT_CHANNELS=slack
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class DailyReportSettings(BaseSettings):
    """
    Daily autonomous operation report task settings.

    Defines retry strategy and default notification channel
    settings for generate_daily_autonomous_report_task.
    """

    model_config = make_settings_config("BALDUR_DAILY_REPORT_")

    # ==========================================================================
    # Master Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable/disable daily report generation. When False, "
        "both direct calls and Celery tasks return immediately.",
    )

    # ==========================================================================
    # Celery Task Retry Settings (generate_daily_autonomous_report_task)
    # ==========================================================================
    max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Maximum retry count for daily report generation task",
    )
    retry_delay: int = Field(
        default=300,
        ge=30,
        le=1800,
        description="Retry delay for daily report generation task (seconds)",
    )

    # ==========================================================================
    # Report Default Settings
    # ==========================================================================
    default_channels: list[str] = Field(
        default_factory=lambda: ["slack"],
        description="Default notification channel list",
    )
    default_hour: int = Field(
        default=9,
        ge=0,
        le=23,
        description="Default hour for daily report generation (0-23)",
    )
    default_minute: int = Field(
        default=0,
        ge=0,
        le=59,
        description="Default minute for daily report generation (0-59)",
    )

    # ==========================================================================
    # Cache Settings
    # ==========================================================================
    cache_ttl: int = Field(
        default=172800,
        ge=3600,
        le=604800,
        description="Daily report cache TTL (seconds). Default 172800 = 2 days",
    )
    max_entries_per_day: int = Field(
        default=5000,
        ge=100,
        le=50000,
        description="Maximum entries per day in daily report list",
    )

    # ==========================================================================
    # Persistence Settings (Phase 2 — 90-day retention)
    # ==========================================================================
    keep_reports_days: int = Field(
        default=90,
        ge=1,
        le=365,
        description="Report retention period in days for state backend persistence",
    )

    # ==========================================================================
    # Shadow PRO Insights Visibility (impl 452)
    # ==========================================================================
    shadow_pro_mode: Literal["auto", "daily", "weekly", "off"] = Field(
        default="auto",
        description=(
            "Visibility policy for the '💡 PRO Insights' upsell block. "
            "'auto' renders daily during the first 30 days then on the "
            "install-date weekly anniversary; 'daily' always renders; "
            "'weekly' renders on the weekly anniversary only; 'off' "
            "suppresses the block. Paying PRO customers are always "
            "auto-suppressed regardless of this value."
        ),
    )


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_daily_report_settings() -> "DailyReportSettings":
    """
    Return cached DailyReportSettings instance.

    Returns:
        DailyReportSettings: Singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.daily_report


def reset_daily_report_settings() -> None:
    """
    Reset cached settings (for testing).

    Call this function to reload settings after changing environment variables.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["daily_report"]
    except KeyError:
        pass
