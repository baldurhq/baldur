"""
Gate Fault Settings - Pydantic v2.

Settings for the Error Budget Gate's internal fault detector.
When the Gate repeatedly fails to reach the Error Budget service (Redis/DB),
it responds fail-open immediately instead of waiting for a timeout each time.

Source:
- services/error_budget_gate/fault_detector.py (GateFaultDetector)

Environment Variables:
    BALDUR_GATE_FAULT_FAILURE_THRESHOLD=5
    BALDUR_GATE_FAULT_RECOVERY_TIMEOUT_SECONDS=30
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class GateFaultSettings(BaseSettings):
    """
    Gate Fault Detector settings.

    Defines fault detection and recovery inside the Error Budget Gate.
    ⚠️ Note: this is NOT the main CircuitBreakerService!
    - GateFaultDetector: internal to the Gate, memory-only (no external
      dependency)
    - CircuitBreakerService: for external API calls, supports distributed
      environments
    """

    model_config = make_settings_config("BALDUR_GATE_FAULT_")

    # ==========================================================================
    # Failure Detection (from fault_detector.py line 46)
    # ==========================================================================
    failure_threshold: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Threshold to transition to DEGRADED state (consecutive failure count)",
    )

    # ==========================================================================
    # Recovery Settings (from fault_detector.py line 46)
    # ==========================================================================
    recovery_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Wait time before recovery attempt from DEGRADED state (seconds)",
    )

    @field_validator("failure_threshold")
    @classmethod
    def validate_failure_threshold(cls, v: int) -> int:
        """Warn when failure_threshold is too small."""
        if v < 3:
            logger.warning(
                "gate_fault_settings.low_consider_using_avoid",
                setting_value=v,
            )
        return v


def get_gate_fault_settings() -> "GateFaultSettings":
    from baldur.settings.root import get_config

    return get_config().meta.gate_fault


def reset_gate_fault_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["gate_fault"]
    except KeyError:
        pass
