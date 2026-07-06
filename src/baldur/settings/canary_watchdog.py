"""
Canary Watchdog Settings - Pydantic v2.

Canary Rollout Watchdog 태스크 설정.
Zombie 롤아웃 감지, 자동 롤백, 자동 프로모션 설정.

Source:
- tasks/canary_watchdog.py

Environment Variables:
    BALDUR_CANARY_WATCHDOG_ZOMBIE_THRESHOLD_MINUTES=30
    BALDUR_CANARY_WATCHDOG_AUTO_ROLLBACK_MINUTES=60
    BALDUR_CANARY_WATCHDOG_MAX_STAGE_DURATION_MINUTES=15
    BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_PROMOTE=true
    BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_ROLLBACK=true
    BALDUR_CANARY_WATCHDOG_SLACK_CHANNEL=#baldur-alerts
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class CanaryWatchdogSettings(BaseSettings):
    """
    Canary Watchdog 설정.

    Zombie 롤아웃 감지 임계값, 자동 롤백/프로모션, 알림 설정을 정의합니다.
    """

    model_config = make_settings_config("BALDUR_CANARY_WATCHDOG_")

    # ==========================================================================
    # Zombie Detection (from canary_watchdog.py line 64)
    # ==========================================================================
    zombie_threshold_minutes: int = Field(
        default=30,
        ge=5,
        le=240,
        description="Time to consider a rollout as stalled/zombie (minutes)",
    )

    # ==========================================================================
    # Auto Rollback (from canary_watchdog.py line 65)
    # ==========================================================================
    auto_rollback_after_minutes: int = Field(
        default=60,
        ge=10,
        le=480,
        description="Wait time before automatic rollback (minutes)",
    )

    # ==========================================================================
    # Stage Duration (from canary_watchdog.py line 66)
    # ==========================================================================
    max_stage_duration_minutes: int = Field(
        default=15,
        ge=1,
        le=120,
        description="Maximum duration per stage (minutes)",
    )

    # ==========================================================================
    # Feature Toggles
    # ==========================================================================
    enable_auto_promote: bool = Field(
        default=True,
        description="Enable automatic promotion",
    )
    enable_auto_rollback: bool = Field(
        default=True,
        description="Enable automatic rollback for zombies",
    )
    notification_enabled: bool = Field(
        default=True,
        description="Enable Slack notifications",
    )

    # ==========================================================================
    # Notification (from canary_watchdog.py line 70)
    # ==========================================================================
    slack_channel: str = Field(
        default="#baldur-alerts",
        min_length=1,
        max_length=100,
        description="Notification Slack channel",
    )

    @model_validator(mode="after")
    def validate_timing(self) -> "CanaryWatchdogSettings":
        """auto_rollback이 zombie_threshold보다 큰지 확인."""
        if self.auto_rollback_after_minutes <= self.zombie_threshold_minutes:
            raise ValueError(
                f"auto_rollback_after_minutes ({self.auto_rollback_after_minutes}) "
                f"must be greater than zombie_threshold_minutes ({self.zombie_threshold_minutes})"
            )
        return self


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_canary_watchdog_settings() -> "CanaryWatchdogSettings":
    """
    캐시된 CanaryWatchdogSettings 인스턴스 반환.

    Returns:
        CanaryWatchdogSettings: 싱글톤 인스턴스
    """
    from baldur.settings.root import get_config

    return get_config().services_group.canary_watchdog


def reset_canary_watchdog_settings() -> None:
    """
    캐시된 설정 초기화 (테스트용).

    환경 변수 변경 후 설정을 다시 로드하려면 이 함수를 호출하세요.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["canary_watchdog"]
    except KeyError:
        pass
