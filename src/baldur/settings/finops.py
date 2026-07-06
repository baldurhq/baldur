"""
FinOps Settings - Pydantic v2.

FinOps DNA service operation cost unit prices, budget defaults,
and CostTier classification threshold settings.
Externalizes hardcoded values as environment variables.

Source:
- services/finops/service.py (operation costs, tier thresholds, alert dedup)
- services/finops/models.py (budget defaults)
- api/django/views/finops.py (duplicated budget defaults)

Environment Variables:
    BALDUR_FINOPS_ENABLED=true
    BALDUR_FINOPS_COST_RETRY=0.001
    BALDUR_FINOPS_COST_CIRCUIT_BREAKER_CHECK=0.0001
    BALDUR_FINOPS_COST_DLQ_ENQUEUE=0.005
    BALDUR_FINOPS_COST_DLQ_REPLAY=0.01
    BALDUR_FINOPS_COST_HEALTH_CHECK=0.0001
    BALDUR_FINOPS_COST_ROLLBACK=0.05
    BALDUR_FINOPS_COST_EMERGENCY_MODE=0.10
    BALDUR_FINOPS_COST_CHAOS_TEST=0.02
    BALDUR_FINOPS_COST_FALLBACK=0.001
    BALDUR_FINOPS_DEFAULT_MAX_BUDGET=10.00
    BALDUR_FINOPS_DEFAULT_ALERT_THRESHOLD=0.8
    BALDUR_FINOPS_DEFAULT_HARD_LIMIT=true
    BALDUR_FINOPS_DEFAULT_RESET_PERIOD=daily
    BALDUR_FINOPS_TIER_LOW_THRESHOLD=0.001
    BALDUR_FINOPS_TIER_MEDIUM_THRESHOLD=0.01
    BALDUR_FINOPS_TIER_HIGH_THRESHOLD=0.10
    BALDUR_FINOPS_MAX_CHAOS_WEIGHT_MULTIPLIER=10.0
    BALDUR_FINOPS_ALERT_DEDUP_COUNT=10
"""

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import Probability

ResetPeriod = Literal["daily", "weekly", "monthly"]


class FinOpsSettings(BaseSettings):
    """
    FinOps DNA service settings.

    Defines operation cost unit prices, budget defaults, and CostTier
    classification thresholds.
    """

    model_config = make_settings_config("BALDUR_FINOPS_")

    # ==========================================================================
    # Feature Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="FinOps service activation toggle",
    )

    # ==========================================================================
    # Operation Costs (USD per operation)
    # ==========================================================================
    cost_retry: Decimal = Field(
        default=Decimal("0.001"),
        ge=0,
        description="Retry operation cost",
    )
    cost_circuit_breaker_check: Decimal = Field(
        default=Decimal("0.0001"),
        ge=0,
        description="Circuit Breaker check cost",
    )
    cost_dlq_enqueue: Decimal = Field(
        default=Decimal("0.005"),
        ge=0,
        description="DLQ Enqueue cost",
    )
    cost_dlq_replay: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        description="DLQ Replay cost",
    )
    cost_health_check: Decimal = Field(
        default=Decimal("0.0001"),
        ge=0,
        description="Health Check cost",
    )
    cost_rollback: Decimal = Field(
        default=Decimal("0.05"),
        ge=0,
        description="Rollback cost",
    )
    cost_emergency_mode: Decimal = Field(
        default=Decimal("0.10"),
        ge=0,
        description="Emergency Mode cost",
    )
    cost_chaos_test: Decimal = Field(
        default=Decimal("0.02"),
        ge=0,
        description="Chaos Test cost",
    )
    cost_fallback: Decimal = Field(
        default=Decimal("0.001"),
        ge=0,
        description="Fallback cost for unknown operations",
    )

    # ==========================================================================
    # Budget Defaults
    # ==========================================================================
    default_max_budget: Decimal = Field(
        default=Decimal("10.00"),
        ge=0,
        description="Default max budget per stage (USD)",
    )
    default_alert_threshold: Probability = Field(
        default=0.8,
        description="Default alert threshold (0.0 ~ 1.0)",
    )
    default_hard_limit: bool = Field(
        default=True,
        description="Block on budget exceeded by default",
    )
    default_reset_period: ResetPeriod = Field(
        default="daily",
        description="Default budget reset period",
    )

    # ==========================================================================
    # CostTier Classification Thresholds
    # ==========================================================================
    tier_low_threshold: Decimal = Field(
        default=Decimal("0.001"),
        ge=0,
        description="LOW tier upper threshold",
    )
    tier_medium_threshold: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        description="MEDIUM tier upper threshold",
    )
    tier_high_threshold: Decimal = Field(
        default=Decimal("0.10"),
        ge=0,
        description="HIGH tier upper threshold",
    )

    # ==========================================================================
    # Chaos Budget
    # ==========================================================================
    max_chaos_weight_multiplier: float = Field(
        default=10.0,
        ge=1.0,
        description="Max domain weight multiplier (explosion prevention)",
    )

    # ==========================================================================
    # Alert Deduplication
    # ==========================================================================
    alert_dedup_count: int = Field(
        default=10,
        ge=1,
        description="Recent alert count to check for deduplication",
    )

    # ==========================================================================
    # Validators
    # ==========================================================================
    @model_validator(mode="after")
    def validate_tier_ordering(self) -> "FinOpsSettings":
        """Tier threshold ordering: low < medium < high."""
        if not (
            self.tier_low_threshold
            < self.tier_medium_threshold
            < self.tier_high_threshold
        ):
            raise ValueError("Tier thresholds must be ordered: low < medium < high")
        return self


def build_operation_costs(settings: FinOpsSettings) -> dict[str, Decimal]:
    """Build operation costs dict from Settings."""
    return {
        "retry": settings.cost_retry,
        "circuit_breaker_check": settings.cost_circuit_breaker_check,
        "dlq_enqueue": settings.cost_dlq_enqueue,
        "dlq_replay": settings.cost_dlq_replay,
        "health_check": settings.cost_health_check,
        "rollback": settings.cost_rollback,
        "emergency_mode": settings.cost_emergency_mode,
        "chaos_test": settings.cost_chaos_test,
    }


def get_finops_settings() -> "FinOpsSettings":
    from baldur.settings.root import get_config

    return get_config().services_group.finops


def reset_finops_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["finops"]
    except KeyError:
        pass
