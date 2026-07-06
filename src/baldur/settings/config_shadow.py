"""
Config Shadow Evaluation Settings.

Shadow Evaluation 및 Shadow Gate 관련 설정.

Environment Variables:
    BALDUR_CONFIG_SHADOW_GATE_ENABLED=true
    BALDUR_CONFIG_SHADOW_REQUIRE_EVALUATION=false  # blocking path dormant until v1.1; True is honored-but-skipped while dormant
    BALDUR_CONFIG_SHADOW_DEFAULT_TIME_WINDOW_HOURS=336
    BALDUR_CONFIG_SHADOW_MIN_CONFIDENCE=0.3
    BALDUR_CONFIG_SHADOW_BYPASS_MIN_REASON_LENGTH=10
    BALDUR_CONFIG_SHADOW_EVALUATION_TTL_HOURS=1.0
    BALDUR_CONFIG_SHADOW_BLOCK_ON_LOW_CONFIDENCE=false
    BALDUR_CONFIG_SHADOW_LIVE_EVALUATION_ENABLED=false
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ConfigShadowSettings(BaseSettings):
    """Config Shadow Evaluation 설정."""

    model_config = make_settings_config("BALDUR_CONFIG_SHADOW_")

    gate_enabled: bool = Field(
        default=True,
        description="Enable/disable shadow gate",
    )
    require_evaluation: bool = Field(
        default=False,
        description=(
            "Require a shadow evaluation before start_rollout() can proceed. "
            "The blocking path is dormant until v1.1: no production trigger "
            "populates rollout-linked evaluations yet, so while dormant True is "
            "honored-but-skipped (warning only, never blocks) and cannot brick "
            "start_rollout(). False: warning only if no evaluation (default). "
            "Auto-yields to an active block once the v1.1 evaluation trigger is wired."
        ),
    )
    default_time_window_hours: int = Field(
        default=336,
        ge=24,
        le=720,
        description="Default analysis time window (336 = 14 days)",
    )
    min_confidence: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Add warning when confidence is below this value",
    )
    bypass_min_reason_length: int = Field(
        default=10,
        ge=5,
        le=500,
        description="Minimum length for bypass_shadow_reason",
    )
    evaluation_ttl_hours: float = Field(
        default=1.0,
        ge=0.25,
        le=24.0,
        description="Evaluation result validity time (default 1 hour). Re-evaluation required when exceeded.",
    )
    block_on_low_confidence: bool = Field(
        default=False,
        description=(
            "True: block start_rollout() when confidence < min_confidence. "
            "False: warning only (default). Switch to True after system matures."
        ),
    )
    confidence_graduation_target_events: int = Field(
        default=50,
        ge=20,
        le=500,
        description="Minimum event count to trigger re-evaluation",
    )
    live_evaluation_enabled: bool = Field(
        default=False,
        description=(
            "Enable live canary evaluation on promote(). "
            "Switch to True after TimeSeriesMetricsProvider implementation is registered."
        ),
    )


def get_config_shadow_settings() -> "ConfigShadowSettings":
    from baldur.settings.root import get_config

    return get_config().adapters.config_shadow


def reset_config_shadow_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["config_shadow"]
    except KeyError:
        pass
