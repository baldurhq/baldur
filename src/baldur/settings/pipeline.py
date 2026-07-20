"""
Adaptive Pipeline Settings - Pydantic v2.

Load-adaptive pipeline settings.
Integrates with GracefulDegradation to switch pipelines automatically based on
system load.

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
    Adaptive pipeline settings.

    With adaptive_enabled=False (the default), standard_pipeline is always used.
    With adaptive_enabled=True, the minimal/standard/ha pipeline is selected
    automatically based on the request's tier_id and the system load.

    Attributes:
        adaptive_enabled: Whether the adaptive pipeline is enabled
        hot_path_tiers: Tiers the minimal pipeline applies to
        audit_sampling_rate: Audit sampling rate of the minimal pipeline
            (1.0=100%)
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
        """Warn when the sampling rate is extreme."""
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
