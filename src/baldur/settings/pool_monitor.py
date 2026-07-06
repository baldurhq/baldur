"""
Pool Monitor Settings - Pydantic v2.

DB Connection Pool 모니터링 및 누수 감지 설정.

Source:
- core/pool_monitor.py
- core/connection_health.py

Environment Variables:
    BALDUR_POOL_MONITOR_WARNING_THRESHOLD=70.0
    BALDUR_POOL_MONITOR_CRITICAL_THRESHOLD=90.0
    BALDUR_POOL_MONITOR_LEAK_THRESHOLD_SECONDS=300.0
    BALDUR_POOL_MONITOR_MAX_HISTORY=5000
    BALDUR_POOL_MONITOR_CONNECTION_FAILURE_THRESHOLD=3
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import SmallCount


class PoolMonitorSettings(BaseSettings):
    """
    Connection Pool 모니터링 설정.

    Pool 사용률 임계값, 누수 감지 기준 등을 정의합니다.
    """

    model_config = make_settings_config("BALDUR_POOL_MONITOR_")

    # ==========================================================================
    # Master Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable/disable pool monitoring. When False, monitor "
        "creation via from_settings() returns a disabled instance.",
    )

    # ==========================================================================
    # Health Thresholds (from core/pool_monitor.py lines 102-104)
    # ==========================================================================
    warning_threshold: float = Field(
        default=70.0,
        ge=10.0,
        le=95.0,
        description="Pool utilization warning threshold (%)",
    )
    critical_threshold: float = Field(
        default=90.0,
        ge=50.0,
        le=100.0,
        description="Pool utilization critical threshold (%)",
    )

    # ==========================================================================
    # Leak Detection (from core/pool_monitor.py line 105)
    # ==========================================================================
    leak_threshold_seconds: float = Field(
        default=300.0,
        ge=30.0,
        le=3600.0,
        description="Suspected connection leak threshold time (seconds). Default 5 minutes.",
    )

    # ==========================================================================
    # History Settings (from core/pool_monitor.py line 116)
    # ==========================================================================
    max_history: int = Field(
        default=5000,
        ge=10,
        le=10000,
        description="Maximum statistics history entries for trend analysis. "
        "Default 5,000 covers 72 hours at 60-second intervals.",
    )

    # ==========================================================================
    # Connection Health Monitor (from core/connection_health.py line 108)
    # ==========================================================================
    connection_failure_threshold: SmallCount = Field(
        default=3,
        description="Consecutive failure count to mark as UNHEALTHY",
    )

    @model_validator(mode="after")
    def validate_thresholds(self) -> "PoolMonitorSettings":
        """warning_threshold가 critical_threshold보다 작은지 확인."""
        if self.warning_threshold >= self.critical_threshold:
            raise ValueError(
                f"warning_threshold ({self.warning_threshold}) must be less than "
                f"critical_threshold ({self.critical_threshold})"
            )
        return self


def get_pool_monitor_settings() -> "PoolMonitorSettings":
    from baldur.settings.root import get_config

    return get_config().core.pool_monitor


def reset_pool_monitor_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["pool_monitor"]
    except KeyError:
        pass
