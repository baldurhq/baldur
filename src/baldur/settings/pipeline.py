"""
Adaptive Pipeline Settings - Pydantic v2.

부하 적응형 파이프라인 설정.
GracefulDegradation 연동으로 시스템 부하에 따라 자동으로 파이프라인을 전환한다.

Environment Variables:
    BALDUR_PIPELINE_ADAPTIVE_ENABLED=false
    BALDUR_PIPELINE_HOT_PATH_TIERS=["non_essential"]
    BALDUR_PIPELINE_AUDIT_SAMPLING_RATE=1.0
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class PipelineSettings(BaseSettings):
    """
    적응형 파이프라인 설정.

    adaptive_enabled=False(기본값)이면 항상 standard_pipeline을 사용한다.
    adaptive_enabled=True이면 요청의 tier_id와 시스템 부하에 따라
    minimal/standard/ha 파이프라인을 자동 선택한다.

    Attributes:
        adaptive_enabled: 적응형 파이프라인 활성화 여부
        hot_path_tiers: minimal 파이프라인을 적용할 tier 목록
        audit_sampling_rate: minimal 파이프라인의 감사 샘플링 비율 (1.0=100%)
    """

    model_config = make_settings_config("BALDUR_PIPELINE_")

    adaptive_enabled: bool = Field(
        default=False,
        description="Enable adaptive pipeline. If False, always uses standard pipeline",
    )

    hot_path_tiers: list[str] = Field(
        default=["non_essential"],
        description="List of tiers to apply the minimal pipeline",
    )

    audit_sampling_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Audit sampling rate for minimal pipeline (1.0=100%, 0.01=1%)",
    )

    @field_validator("audit_sampling_rate")
    @classmethod
    def validate_audit_sampling_rate(cls, v: float) -> float:
        """샘플링 비율이 극단적이면 경고."""
        if 0.0 < v < 0.01:
            logger.warning(
                "pipeline_settings.very_low_audit_sampling",
                setting_value=v,
            )
        return v


def get_pipeline_settings() -> "PipelineSettings":
    from baldur.settings.root import get_config

    return get_config().meta.pipeline


def reset_pipeline_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["pipeline"]
    except KeyError:
        pass
