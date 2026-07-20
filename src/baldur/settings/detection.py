"""
Detection Settings — Pydantic v2.

Anomaly detection and correlation engine settings.
Makes the previously hardcoded window/threshold values controllable through
environment variables.

Environment Variables:
    BALDUR_DETECTION_ANOMALY_WINDOW_SIZE=100
    BALDUR_DETECTION_ANOMALY_ZSCORE_THRESHOLD=3.0
    BALDUR_DETECTION_ANOMALY_WINDOW_MAX_AGE_SECONDS=300.0
    BALDUR_DETECTION_CORRELATION_WINDOW_SIZE=100
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class DetectionSettings(BaseSettings):
    """Anomaly detection and correlation engine settings."""

    model_config = make_settings_config("BALDUR_DETECTION_")

    anomaly_window_size: int = Field(
        default=100,
        ge=10,
        le=10000,
        description="Number of data points for anomaly detection sliding window",
    )

    anomaly_zscore_threshold: float = Field(
        default=3.0,
        ge=1.0,
        le=10.0,
        description="Z-Score threshold for anomaly detection",
    )

    anomaly_window_max_age_seconds: float = Field(
        default=300.0,
        ge=10.0,
        le=86400.0,
        description="Maximum age (seconds) of data points in anomaly window. "
        "Points older than this are discarded even if window_size not reached.",
    )

    correlation_window_size: int = Field(
        default=100,
        ge=10,
        le=10000,
        description="Number of events for co-occurrence correlation window",
    )


def get_detection_settings() -> "DetectionSettings":
    """Single entry point via the root settings (SSOT)."""
    from baldur.settings.root import get_config

    return get_config().metrics_group.detection


def reset_detection_settings() -> None:
    """Delegate to the root reset (for tests)."""
    from baldur.settings.root import get_config

    try:
        del get_config().metrics_group.__dict__["detection"]
    except KeyError:
        pass
