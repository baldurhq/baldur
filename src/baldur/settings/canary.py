"""
Canary Settings - Pydantic v2.

Canary rollout configuration.
Gradual deployment, concurrency control, and cross-cluster notification settings.

Source:
- services/canary/service.py
- services/canary/cross_cluster.py
- services/canary/locking.py

Environment Variables:
    BALDUR_CANARY_ROLLOUT_TTL_DAYS=7
    BALDUR_CANARY_LOCK_TIMEOUT_MINUTES=30
    BALDUR_CANARY_DEFAULT_EXPIRY_HOURS=24
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.validators import warn_above, warn_below

# Valid service-tier ids for canary pass-criteria tier floors. Mirrors
# PassCriteria.for_tier / apply_tier_floor and the error-budget gate's
# per-tier thresholds.
_VALID_TIER_IDS = frozenset({"critical", "standard", "non_essential"})


class CanarySettings(BaseSettings):
    """
    Canary Rollout settings.

    Defines gradual deployment, lock timeout, and cross-cluster
    communication settings.
    """

    model_config = make_settings_config("BALDUR_CANARY_")

    # ==========================================================================
    # Rollout Settings (from service.py line 103)
    # ==========================================================================
    rollout_ttl_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Rollout data retention period (days)",
    )

    # ==========================================================================
    # Locking Settings (from locking.py line 81)
    # ==========================================================================
    lock_timeout_minutes: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Config lock auto-expiry time (minutes)",
    )

    # ==========================================================================
    # Propagation Request Settings (from cross_cluster.py line 599)
    # ==========================================================================
    default_expiry_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Default expiry time for propagation requests (hours)",
    )

    # ==========================================================================
    # API View Settings (from views/canary.py)
    # ==========================================================================
    default_completed_rollouts_limit: int = Field(
        default=20,
        ge=5,
        le=100,
        description="Default limit for completed rollouts list query",
    )

    # ==========================================================================
    # Propagation Request TTL (from cross_cluster.py line 848)
    # ==========================================================================
    propagation_ttl: int = Field(
        default=604800,
        ge=3600,
        le=2592000,
        description="Propagation request Redis TTL (seconds). Default 604800 = 7 days",
    )

    # ==========================================================================
    # Service-tier resolution for pass-criteria tier floors (automatic, no
    # per-promote tier_id argument). _resolve_tier_id applies precedence:
    # explicit promote(tier_id=...) -> tier_map[config_type] -> default_tier.
    # ==========================================================================
    tier_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "config_type -> tier_id mapping for canary tier-floor resolution. "
            "tier_id must be one of critical/standard/non_essential. "
            "Set via BALDUR_CANARY_TIER_MAP as JSON, e.g. "
            '{"circuit_breaker": "critical"}'
        ),
    )
    default_tier: str = Field(
        default="standard",
        description=(
            "Fallback tier_id when config_type is not in tier_map "
            "(critical/standard/non_essential). 'standard' is behavior-preserving: "
            "PassCriteria defaults equal the standard floor."
        ),
    )

    @field_validator("lock_timeout_minutes")
    @classmethod
    def _warn_lock_timeout(cls, v: int) -> int:
        """Warn when lock_timeout is too long (slow zombie-lock cleanup) or too
        short relative to the 5-min watchdog renewal cadence (lock may lapse
        between renewal passes; healed by re-acquire but worth flagging)."""
        warn_above(60, "canary_settings.high_consider_using_responsiveness")(v)
        return warn_below(15, "canary_settings.low_below_renewal_cadence_margin")(v)

    @field_validator("default_tier")
    @classmethod
    def _validate_default_tier(cls, v: str) -> str:
        """Constrain default_tier to the known service-tier vocabulary."""
        if v not in _VALID_TIER_IDS:
            raise ValueError(
                f"default_tier must be one of {sorted(_VALID_TIER_IDS)}, got {v!r}"
            )
        return v

    @field_validator("tier_map")
    @classmethod
    def _validate_tier_map(cls, v: dict[str, str]) -> dict[str, str]:
        """Constrain every mapped tier_id to the known service-tier vocabulary."""
        invalid = {
            config_type: tier
            for config_type, tier in v.items()
            if tier not in _VALID_TIER_IDS
        }
        if invalid:
            raise ValueError(
                f"tier_map values must be one of {sorted(_VALID_TIER_IDS)}; "
                f"invalid entries: {invalid}"
            )
        return v


def get_canary_settings() -> "CanarySettings":
    from baldur.settings.root import get_config

    return get_config().services_group.canary


def reset_canary_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["canary"]
    except KeyError:
        pass
