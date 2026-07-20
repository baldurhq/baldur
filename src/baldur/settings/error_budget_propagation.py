"""
Error Budget Propagation Settings - Pydantic v2.

Manages cross-domain Error Budget propagation settings.

Replaces:
- services/error_budget/constants.py:DEFAULT_PROPAGATION_DECAY
- services/error_budget/constants.py:DEFAULT_PROPAGATION_MAX_HOPS
- services/error_budget/propagation.py:PropagationConfig

Environment Variables:
    BALDUR_ERROR_BUDGET_PROPAGATION_DECAY_PER_HOP=0.5
    BALDUR_ERROR_BUDGET_PROPAGATION_MAX_HOPS=3
    BALDUR_ERROR_BUDGET_PROPAGATION_BASE_MULTIPLIER=5.0
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    TinyCount,
)
from baldur.settings.validators import warn_above


class ErrorBudgetPropagationSettings(BaseSettings):
    """
    Domain propagation settings.

    Controls how error budget impact is propagated from a faulting domain
    to its dependent domains.

    Attributes:
        decay_per_hop: Decay rate per hop (1-hop: 50% decay)
        max_hops: Maximum propagation hops
        base_multiplier: Base multiplier for the faulting domain
        min_multiplier: Minimum multiplier (decay floor)
        enabled: Enable propagation feature
    """

    model_config = make_settings_config("BALDUR_ERROR_BUDGET_PROPAGATION_")

    # ==========================================================================
    # Core Propagation Settings (from constants.py)
    # ==========================================================================
    decay_per_hop: float = Field(
        default=0.5,
        ge=0.1,
        le=1.0,
        description="Decay rate per hop (1-hop: 50% decay, 2-hop: 25% decay)",
    )

    max_hops: TinyCount = Field(
        default=3,
        description="Maximum propagation hops (prevents circular references and limits performance impact)",
    )

    base_multiplier: float = Field(
        default=5.0,
        ge=1.0,
        le=20.0,
        description="Base multiplier for the faulting domain",
    )

    min_multiplier: float = Field(
        default=1.0,
        ge=0.1,
        le=5.0,
        description="Minimum multiplier (decay floor)",
    )

    # ==========================================================================
    # Enable/Disable
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable propagation feature",
    )

    @field_validator("min_multiplier")
    @classmethod
    def _warn_min_multiplier(cls, v: float, info) -> float:
        """Warn if min_multiplier is unusually high."""
        return warn_above(5.0, "safe_default.high_consider_lower_values")(v)


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_error_budget_propagation_settings() -> "ErrorBudgetPropagationSettings":
    """Get cached ErrorBudgetPropagationSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.error_budget_propagation


def reset_error_budget_propagation_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["error_budget_propagation"]
    except KeyError:
        pass
