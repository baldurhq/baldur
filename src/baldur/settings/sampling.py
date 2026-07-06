"""
Sampling Settings - Pydantic v2.

확률적 체인 검증을 위한 샘플링 설정입니다.

Replaces:
- audit/performance/sampling.py:SamplingConfig

Environment Variables:
    BALDUR_SAMPLING_SAMPLE_RATE=0.1
    BALDUR_SAMPLING_MIN_SAMPLES=10
    BALDUR_SAMPLING_MAX_SAMPLES=1000
    BALDUR_SAMPLING_FULL_VERIFY_ON_FAILURE=true
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class SamplingSettings(BaseSettings):
    """
    샘플링 검증 설정.

    전체 체인 검증 대신 확률적 샘플링을 사용하여 성능을 개선합니다.
    O(n) → O(k) 복잡도 감소 (k = n × sample_rate).

    Attributes:
        sample_rate: 샘플링 비율 (0.1 = 10%)
        min_samples: 최소 샘플 수 (작은 데이터셋 보호)
        max_samples: 최대 샘플 수 (성능 제한)
        full_verify_on_failure: 샘플 검증 실패 시 전체 검증 수행 여부
    """

    model_config = make_settings_config("BALDUR_SAMPLING_")

    # ==========================================================================
    # Core Sampling Settings (from audit/performance/sampling.py SamplingConfig)
    # ==========================================================================
    sample_rate: float = Field(
        default=0.1,
        ge=0.01,
        le=1.0,
        description="Sampling rate (0.1 = 10%). Higher is more accurate but slower",
    )

    min_samples: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Minimum sample count. Ensures reliability for small datasets",
    )

    max_samples: int = Field(
        default=1000,
        ge=10,
        le=100000,
        description="Maximum sample count. Limits performance impact on large datasets",
    )

    full_verify_on_failure: bool = Field(
        default=True,
        description="Whether to perform full chain verification on sample verification failure",
    )

    @field_validator("max_samples")
    @classmethod
    def validate_max_samples(cls, v: int, info) -> int:
        """max_samples가 min_samples보다 커야 함."""
        # Note: min_samples 기본값(10)보다 작으면 경고
        if v < 10:
            logger.warning(
                "safe_default.very_low_reduce_accuracy",
                setting_value=v,
            )
        return v

    @field_validator("sample_rate")
    @classmethod
    def validate_sample_rate(cls, v: float) -> float:
        """샘플링 비율 경고."""
        if v < 0.05:
            logger.warning(
                "safe_default.very_low_miss_issues",
                setting_value=v,
            )
        if v > 0.5:
            logger.warning(
                "safe_default.high_impact_performance",
                setting_value=v,
            )
        return v


def get_sampling_settings() -> "SamplingSettings":
    from baldur.settings.root import get_config

    return get_config().testing.sampling


def reset_sampling_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().testing.__dict__["sampling"]
    except KeyError:
        pass
