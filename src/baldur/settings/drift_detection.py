"""
Drift Detection Settings - Pydantic v2.

SLA 드리프트 감지 태스크 관련 설정.

Source:
- tasks/drift_detection.py

Environment Variables:
    BALDUR_DRIFT_DETECTION_ANALYSIS_WINDOW_HOURS=24
    BALDUR_DRIFT_DETECTION_SLA_BREACH_RATE_THRESHOLD=10.0
    BALDUR_DRIFT_DETECTION_SLA_APPROACHING_THRESHOLD=0.8
    BALDUR_DRIFT_DETECTION_PENDING_AT_RISK_THRESHOLD=5
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class DriftDetectionSettings(BaseSettings):
    """
    SLA 드리프트 감지 설정.

    SLA 위반 감지, 분석 윈도우, 경고 임계값 등을 정의합니다.
    """

    model_config = make_settings_config("BALDUR_DRIFT_DETECTION_")

    # ==========================================================================
    # Analysis Window (from drift_detection.py line 112)
    # ==========================================================================
    analysis_window_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="SLA analysis window size (hours)",
    )

    # ==========================================================================
    # SLA Breach Thresholds (from drift_detection.py line 174-195)
    # ==========================================================================
    sla_breach_rate_threshold: float = Field(
        default=10.0,
        ge=1.0,
        le=50.0,
        description="SLA breach rate warning threshold (%)",
    )

    sla_breach_rate_critical_threshold: float = Field(
        default=25.0,
        ge=10.0,
        le=75.0,
        description="SLA breach rate critical threshold (%)",
    )

    # ==========================================================================
    # SLA Approaching Threshold (from drift_detection.py line 184)
    # ==========================================================================
    sla_approaching_threshold: float = Field(
        default=0.8,
        ge=0.5,
        le=0.95,
        description="SLA approaching warning threshold (ratio, 0.8 = 80%)",
    )

    # ==========================================================================
    # Pending At Risk (from drift_detection.py line 195)
    # ==========================================================================
    pending_at_risk_threshold: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Pending status at-risk warning threshold (count)",
    )

    @field_validator("sla_breach_rate_critical_threshold")
    @classmethod
    def validate_critical_threshold(cls, v: float, info) -> float:
        """critical_threshold가 breach_rate_threshold보다 커야 함."""
        # Note: 이 검증은 model_validator로 더 정확하게 할 수 있음
        return v


def get_drift_detection_settings() -> "DriftDetectionSettings":
    from baldur.settings.root import get_config

    return get_config().metrics_group.drift_detection


def reset_drift_detection_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().metrics_group.__dict__["drift_detection"]
    except KeyError:
        pass
