"""
Notification Channel Settings - Pydantic v2.

알림 채널별 Rate Limiting 및 재시도 정책 설정입니다.

Replaces:
- services/unified_notification.py:채널 매핑
- core/safe_defaults.py:notification 관련 설정
- notification_policy.py:cooldown_seconds

Environment Variables:
    BALDUR_NOTIFICATION_CHANNEL_RATE_LIMIT_PER_MINUTE=60
    BALDUR_NOTIFICATION_CHANNEL_MAX_RETRY=3
    BALDUR_NOTIFICATION_CHANNEL_COOLDOWN_SECONDS=300

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 3 [15])
- docs/baldur/middleware_system/91_CONFIG_INVENTORY.md §6.33, §8.6
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    LargeCount,
)
from baldur.settings.validators import warn_above


class NotificationChannelSettings(BaseSettings):
    """
    알림 채널 설정.

    심각도별 채널 매핑 및 Rate Limiting을 관리합니다.

    Features:
    - 심각도별 채널 라우팅 (CRITICAL → slack,email,pagerduty)
    - Rate Limiting으로 알림 폭주 방지
    - 재시도 정책
    - 쿨다운으로 중복 알림 방지
    """

    model_config = make_settings_config("BALDUR_NOTIFICATION_CHANNEL_")

    # ==========================================================================
    # Rate Limiting (from safe_defaults.py, notification_config.py)
    # ==========================================================================
    rate_limit_per_minute: LargeCount = Field(
        default=60,
        description="Maximum notifications per minute",
    )

    rate_limit_per_hour: int = Field(
        default=300,
        ge=10,
        le=5000,
        description="Maximum notifications per hour",
    )

    # ==========================================================================
    # Retry Settings (from notification_config.py)
    # ==========================================================================
    max_retry: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retry count for notification delivery",
    )

    retry_delay_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Retry interval (seconds)",
    )

    # ==========================================================================
    # Cooldown Settings (from notification_policy.py#L100)
    # ==========================================================================
    cooldown_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Cooldown before resending the same notification (seconds)",
    )

    # ==========================================================================
    # Escalation Settings (from models.py#L351)
    # ==========================================================================
    escalate_on_emergency: bool = Field(
        default=True,
        description="Auto-escalate on emergency situations",
    )

    @field_validator("rate_limit_per_minute")
    @classmethod
    def _warn_rate_limit(cls, v: int) -> int:
        """Rate limit이 너무 높으면 경고."""
        return warn_above(100, "notification_channel.rate_limit_high")(v)


def get_notification_channel_settings() -> "NotificationChannelSettings":
    from baldur.settings.root import get_config

    return get_config().adapters.notification_channel


def reset_notification_channel_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["notification_channel"]
    except KeyError:
        pass
