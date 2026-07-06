"""
Backoff Settings - Pydantic v2.

재시도 메커니즘의 Backoff 전략 설정.

Source:
- core/backoff.py

Environment Variables:
    BALDUR_BACKOFF_EXPONENTIAL_BASE_DELAY=1.0
    BALDUR_BACKOFF_EXPONENTIAL_MAX_DELAY=300.0
    BALDUR_BACKOFF_EXPONENTIAL_MULTIPLIER=2.0
    BALDUR_BACKOFF_EXPONENTIAL_JITTER_FACTOR=0.2
    ...
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_BACKOFF_MULTIPLIER,
    STANDARD_BASE_DELAY,
    STANDARD_JITTER_FACTOR,
    STANDARD_MAX_DELAY,
    JitterFactor,
    LongDuration,
    MediumDuration,
    ShortDuration,
    ShortInterval,
    TinyCount,
)
from baldur.settings.validators import warn_above


class BackoffSettings(BaseSettings):
    """
    Backoff 전략 설정.

    Exponential, Linear, Constant, Decorrelated Jitter 전략의
    기본값을 정의합니다.
    """

    model_config = make_settings_config("BALDUR_BACKOFF_")

    # ==========================================================================
    # Exponential Backoff (from core/backoff.py lines 44-49)
    # ==========================================================================
    exponential_base_delay: ShortDuration = Field(
        default=STANDARD_BASE_DELAY,
        description="Exponential backoff base delay (seconds)",
    )
    exponential_max_delay: LongDuration = Field(
        default=STANDARD_MAX_DELAY,
        description="Exponential backoff maximum delay (seconds)",
    )
    exponential_multiplier: float = Field(
        default=STANDARD_BACKOFF_MULTIPLIER,
        ge=1.1,
        le=10.0,
        description="Exponential backoff multiplier",
    )
    exponential_jitter_factor: JitterFactor = Field(
        default=STANDARD_JITTER_FACTOR,
        description="Exponential backoff jitter factor (0.0-1.0)",
    )

    # ==========================================================================
    # Linear Backoff (from core/backoff.py lines 74-80)
    # ==========================================================================
    linear_base_delay: ShortDuration = Field(
        default=STANDARD_BASE_DELAY,
        description="Linear backoff base delay (seconds)",
    )
    linear_increment: ShortDuration = Field(
        default=1.0,
        description="Linear backoff increment (seconds)",
    )
    linear_max_delay: MediumDuration = Field(
        default=60.0,
        description="Linear backoff maximum delay (seconds)",
    )
    linear_jitter_factor: JitterFactor = Field(
        default=0.1,
        description="Linear backoff jitter factor",
    )

    # ==========================================================================
    # Constant Backoff (from core/backoff.py lines 106-109)
    # ==========================================================================
    constant_delay: float = Field(
        default=5.0,
        ge=0.1,
        le=300.0,
        description="Constant backoff fixed delay (seconds)",
    )
    constant_jitter_factor: JitterFactor = Field(
        default=0.1,
        description="Constant backoff jitter factor",
    )

    # ==========================================================================
    # Decorrelated Jitter Backoff (from core/backoff.py lines 126-128)
    # ==========================================================================
    decorrelated_base_delay: ShortDuration = Field(
        default=STANDARD_BASE_DELAY,
        description="Decorrelated jitter backoff base delay (seconds)",
    )
    decorrelated_max_delay: LongDuration = Field(
        default=STANDARD_MAX_DELAY,
        description="Decorrelated jitter backoff maximum delay (seconds)",
    )

    # ==========================================================================
    # Legacy Backoff (from core/backoff.py LegacyBackoffConfig)
    # ==========================================================================
    legacy_base: TinyCount = Field(
        default=4,
        description="Legacy backoff exponential base (4^n)",
    )
    legacy_jitter_percent: int = Field(
        default=25,
        ge=0,
        le=100,
        description="Legacy backoff jitter percent (+/-%)",
    )
    legacy_min_delay: ShortInterval = Field(
        default=1,
        description="Legacy backoff minimum delay (seconds)",
    )

    @field_validator("exponential_max_delay")
    @classmethod
    def _warn_exponential_max_delay(cls, v: float) -> float:
        """max_delay가 너무 크면 경고."""
        return warn_above(600, "backoff_settings.high_consider_using_responsiveness")(v)


def get_backoff_settings() -> "BackoffSettings":
    from baldur.settings.root import get_config

    return get_config().core.backoff


def reset_backoff_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["backoff"]
    except KeyError:
        pass
