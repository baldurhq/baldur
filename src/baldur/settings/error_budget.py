"""
Error Budget Settings - Pydantic v2.

Single Source of Truth for error budget configuration.

Replaces:
- core/config.py:ErrorBudgetConfig (lines 278-319)
- core/safe_defaults.py:SAFE_DEFAULTS["error_budget"]
- core/safe_defaults.py:VALIDATION_RULES["error_budget"]

Environment Variables:
    BALDUR_ERROR_BUDGET_THRESHOLD_HEALTHY=75.0
    BALDUR_ERROR_BUDGET_BURN_RATE_FAST_CRITICAL=14.4

Reference:
- docs/baldur/middleware_system/40_PYDANTIC_CONFIG_MIGRATION.md
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ErrorBudgetSettings(BaseSettings):
    """
    Error Budget thresholds configuration with validation.

    Google SRE recommended values are used as defaults.

    All defaults match core/config.py:ErrorBudgetConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["error_budget"]
    """

    model_config = make_settings_config("BALDUR_ERROR_BUDGET_")

    # ==========================================================================
    # Feature Enable/Disable (SOC2 compliance: must be explicitly disableable)
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable error budget tracking",
    )

    # ==========================================================================
    # Error Budget Thresholds (%) (from core/config.py lines 285-289)
    # Validation rules from core/safe_defaults.py lines 287-294
    # ==========================================================================
    threshold_healthy: float = Field(
        default=75.0,
        ge=50.0,
        le=100.0,
        description="Healthy threshold (%) - 75% or above is normal",
    )
    threshold_caution: float = Field(
        default=50.0,
        ge=20.0,
        le=80.0,
        description="Caution threshold (%) - 50-75% requires attention",
    )
    threshold_warning: float = Field(
        default=20.0,
        ge=5.0,
        le=50.0,
        description="Warning threshold (%) - 20-50% is warning level",
    )
    threshold_critical: float = Field(
        default=0.0,
        ge=0.0,
        le=20.0,
        description="Critical threshold (%) - below 20% is critical",
    )

    # ==========================================================================
    # Burn Rate Thresholds (Google SRE) (from core/config.py lines 291-295)
    # ==========================================================================
    burn_rate_fast_critical: float = Field(
        default=14.4,
        ge=10.0,
        le=50.0,
        description="Fast burn rate critical threshold (2% in 1 hour)",
    )
    burn_rate_fast_warning: float = Field(
        default=6.0,
        ge=3.0,
        le=15.0,
        description="Fast burn rate warning threshold",
    )
    burn_rate_slow_warning: float = Field(
        default=3.0,
        ge=1.0,
        le=10.0,
        description="Slow burn rate warning threshold (5% in 6 hours)",
    )
    burn_rate_slow_info: float = Field(
        default=1.0,
        ge=0.5,
        le=3.0,
        description="Slow burn rate info threshold (normal consumption)",
    )

    # ==========================================================================
    # Fail-Safe Settings (from core/config.py lines 297-299)
    # ==========================================================================
    failsafe_alert_enabled: bool = Field(
        default=False,
        description="Enable alerts when fail-safe is triggered",
    )
    failsafe_cooldown_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Cooldown between consecutive fail-safe alerts",
    )

    # ==========================================================================
    # Heartbeat (Dead Man's Snitch) Settings (from core/config.py lines 305-308)
    # ==========================================================================
    heartbeat_enabled: bool = Field(
        default=False,
        description="Enable heartbeat monitoring",
    )
    heartbeat_interval_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Heartbeat interval in seconds",
    )
    heartbeat_timeout_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Heartbeat timeout - dead judgment if exceeded",
    )

    # ==========================================================================
    # Recovery Notification Settings (from core/config.py lines 314-316)
    # ==========================================================================
    recovery_alert_enabled: bool = Field(
        default=False,
        description="Enable alerts on recovery",
    )
    recovery_alert_include_downtime: bool = Field(
        default=True,
        description="Include downtime information in recovery alerts",
    )

    # ==========================================================================
    # Override Escalation Settings (from core/config.py lines 322-325)
    # ==========================================================================
    escalation_enabled: bool = Field(
        default=False,
        description="Enable override escalation",
    )
    escalation_channel: str = Field(
        default="#governance",
        description="Escalation notification channel",
    )
    escalation_mention: str = Field(
        default="@cto @security",
        description="Mention targets for escalation",
    )

    # ==========================================================================
    # Crisis Multiplier Settings - from services/error_budget/multiplier.py
    # ==========================================================================
    multiplier_cache_ttl: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Crisis multiplier cache TTL (seconds). Default 30s.",
    )
    multiplier_max: float = Field(
        default=10.0,
        ge=1.0,
        le=50.0,
        description="Maximum crisis multiplier. Prevents excessive error budget consumption.",
    )

    # ==========================================================================
    # Exception Budget Weight Settings - Q12 구현
    # ==========================================================================
    weight_combine_policy: str = Field(
        default="MAX",
        pattern=r"^(MAX|SUM|MULTIPLY)$",
        description=(
            "Weight combine policy for EmergencyLevel and ErrorCode. "
            "MAX: take maximum (recommended), SUM: add, MULTIPLY: multiply (not recommended)"
        ),
    )
    exception_weights_json: str | None = Field(
        default=None,
        description=(
            "Per-ErrorCode weight JSON configuration. "
            'Example: {"category_weights": {"SYSTEM": 1.0}, "code_weights": {"SERVICE_TIMEOUT": 0.5}}'
        ),
    )

    # ==========================================================================
    # Validators
    # ==========================================================================
    @field_validator("weight_combine_policy")
    @classmethod
    def validate_weight_combine_policy(cls, v: str) -> str:
        """가중치 결합 정책 검증."""
        valid_policies = {"MAX", "SUM", "MULTIPLY"}
        v_upper = v.upper()
        if v_upper not in valid_policies:
            raise ValueError(f"Invalid policy: {v}. Must be one of {valid_policies}")
        return v_upper


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_error_budget_settings() -> "ErrorBudgetSettings":
    from baldur.settings.root import get_config

    return get_config().slo_group.error_budget


def reset_error_budget_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().slo_group.__dict__["error_budget"]
    except KeyError:
        pass
