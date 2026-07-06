"""
Audit Watchdog Settings - Pydantic v2.

Audit Watchdog (Dead Man's Switch Pattern) 설정.

Source:
- audit/audit_watchdog.py (WatchdogConfig, HeartbeatTarget)

Environment Variables:
    BALDUR_AUDIT_WATCHDOG_HEARTBEAT_INTERVAL_SECONDS=30.0
    BALDUR_AUDIT_WATCHDOG_MISSED_THRESHOLD=3
    BALDUR_AUDIT_WATCHDOG_TIMEOUT_SECONDS=5.0
    BALDUR_AUDIT_WATCHDOG_LOCAL_HEARTBEAT_FILE=
    BALDUR_AUDIT_WATCHDOG_HEARTBEAT_URL=
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import TinyCount
from baldur.settings.validators import warn_below


class AuditWatchdogSettings(BaseSettings):
    """
    Audit Watchdog 설정.

    Dead Man's Switch 패턴으로 감사 시스템 생존 확인.
    주기적으로 heartbeat를 전송하고 외부 모니터링 시스템이 감시.
    """

    model_config = make_settings_config("BALDUR_AUDIT_WATCHDOG_")

    # ==========================================================================
    # Heartbeat Settings (from audit_watchdog.py lines 86-89)
    # ==========================================================================
    heartbeat_interval_seconds: float = Field(
        default=30.0,
        ge=10.0,
        le=300.0,
        description="Heartbeat send interval (seconds)",
    )

    missed_threshold: TinyCount = Field(
        default=3,
        description="Consecutive missed heartbeat threshold",
    )

    # ==========================================================================
    # Target Settings (from audit_watchdog.py HeartbeatTarget line 77)
    # ==========================================================================
    timeout_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Heartbeat request timeout (seconds)",
    )

    # ==========================================================================
    # Local File Heartbeat
    # ==========================================================================
    local_heartbeat_file: str | None = Field(
        default=None,
        description="Local file heartbeat path (works without external services)",
    )

    # ==========================================================================
    # Heartbeat URL
    # ==========================================================================
    heartbeat_url: str | None = Field(
        default=None,
        description="Heartbeat target URL",
    )

    # ==========================================================================
    # Max Age (from audit_watchdog.py line 418)
    # ==========================================================================
    max_age_seconds: float = Field(
        default=60.0,
        ge=30.0,
        le=300.0,
        description="Maximum heartbeat validity time (seconds)",
    )

    @field_validator("heartbeat_interval_seconds")
    @classmethod
    def _warn_heartbeat_interval_seconds(cls, v: float) -> float:
        """heartbeat 주기가 너무 짧으면 경고."""
        return warn_below(15.0, "audit_watchdog_settings.low_consider_using_reduce")(v)


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_audit_watchdog_settings() -> "AuditWatchdogSettings":
    """
    캐시된 AuditWatchdogSettings 인스턴스 반환.

    Returns:
        AuditWatchdogSettings: 싱글톤 인스턴스
    """
    from baldur.settings.root import get_config

    return get_config().audit_group.audit_watchdog


def reset_audit_watchdog_settings() -> None:
    """
    캐시된 Settings 초기화 (테스트용).
    """
    from baldur.settings.root import get_config

    try:
        del get_config().audit_group.__dict__["audit_watchdog"]
    except KeyError:
        pass
