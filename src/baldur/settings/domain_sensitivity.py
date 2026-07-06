"""
Domain Sensitivity Settings - Pydantic v2.

Per-domain Error Budget sensitivity weight settings.
The domain list is not hardcoded; it is managed via settings (env vars / .env).

Replaces:
- services/error_budget/constants.py:DEFAULT_DOMAIN_SENSITIVITY
- services/error_budget/constants.py:DEFAULT_LEVEL_MULTIPLIERS

Environment Variables:
    BALDUR_DOMAIN_SENSITIVITY_DOMAINS='{"payment": 10.0, "order": 5.0}'
    BALDUR_DOMAIN_SENSITIVITY_DEFAULT_SENSITIVITY=1.0

"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

# ==========================================================================
# Per-domain weight defaults (SSOT)
# Can be overridden / extended via env vars without code changes
# ==========================================================================
DEFAULT_DOMAIN_WEIGHTS: dict[str, float] = {
    "payment": 10.0,
    "order": 5.0,
    "inventory": 3.0,
    "notification": 1.5,
    "analytics": 1.0,
}

# Allowed range for domain weights
DOMAIN_WEIGHT_MIN: float = 0.1
DOMAIN_WEIGHT_MAX: float = 100.0


class DomainSensitivitySettings(BaseSettings):
    """
    Per-domain sensitivity weight settings.

    Manages the domain list and weights as a single dict field.
    Adding a new domain requires only editing env vars, no code changes.

    Env var example:
        BALDUR_DOMAIN_SENSITIVITY_DOMAINS='{"payment": 10.0, "logistics": 2.0}'

    Emergency level multipliers:
    - NORMAL: 1.0 (normal operation)
    - LEVEL_1: 1.5 (minor incident)
    - LEVEL_2: 3.0 (major incident)
    - LEVEL_3: 5.0 (severe incident)
    """

    model_config = make_settings_config("BALDUR_DOMAIN_SENSITIVITY_")

    # ==========================================================================
    # Domain Sensitivity Weights - managed as a single dict
    # ==========================================================================
    domains: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_DOMAIN_WEIGHTS),
        description=(
            "Per-domain sensitivity weights. "
            "Env var: BALDUR_DOMAIN_SENSITIVITY_DOMAINS='{\"payment\": 10.0}'"
        ),
    )

    default_sensitivity: float = Field(
        default=1.0,
        ge=DOMAIN_WEIGHT_MIN,
        le=DOMAIN_WEIGHT_MAX,
        description="Default sensitivity for unknown domains",
    )

    # ==========================================================================
    # Emergency Level Multipliers
    # ==========================================================================
    level_multiplier_normal: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        description="NORMAL level multiplier",
    )

    level_multiplier_level_1: float = Field(
        default=1.5,
        ge=1.0,
        le=3.0,
        description="LEVEL_1 multiplier",
    )

    level_multiplier_level_2: float = Field(
        default=3.0,
        ge=1.5,
        le=6.0,
        description="LEVEL_2 multiplier",
    )

    level_multiplier_level_3: float = Field(
        default=5.0,
        ge=3.0,
        le=10.0,
        description="LEVEL_3 multiplier (most severe)",
    )

    @model_validator(mode="after")
    def _validate_domain_weights(self) -> "DomainSensitivitySettings":
        """Validate that all domain weights are within the allowed range."""
        for domain, weight in self.domains.items():
            if not (DOMAIN_WEIGHT_MIN <= weight <= DOMAIN_WEIGHT_MAX):
                raise ValueError(
                    f"Domain '{domain}' weight {weight} is "
                    f"outside the allowed range [{DOMAIN_WEIGHT_MIN}, {DOMAIN_WEIGHT_MAX}]"
                )
        return self

    def get_domain_weight(self, domain: str) -> float:
        """Look up the weight by domain name."""
        return self.domains.get(domain.lower(), self.default_sensitivity)

    def get_level_multiplier(self, level: str) -> float:
        """Look up the multiplier by emergency level."""
        level_upper = level.upper()
        multipliers = {
            "NORMAL": self.level_multiplier_normal,
            "LEVEL_1": self.level_multiplier_level_1,
            "LEVEL_2": self.level_multiplier_level_2,
            "LEVEL_3": self.level_multiplier_level_3,
        }
        return multipliers.get(level_upper, self.level_multiplier_normal)

    def as_domain_dict(self) -> dict[str, float]:
        """Return the domain sensitivity dictionary."""
        return dict(self.domains)

    def as_level_dict(self) -> dict[str, float]:
        """Return the level multiplier dictionary."""
        return {
            "NORMAL": self.level_multiplier_normal,
            "LEVEL_1": self.level_multiplier_level_1,
            "LEVEL_2": self.level_multiplier_level_2,
            "LEVEL_3": self.level_multiplier_level_3,
        }


def get_domain_sensitivity_settings() -> "DomainSensitivitySettings":
    from baldur.settings.root import get_config

    return get_config().security_group.domain_sensitivity


def reset_domain_sensitivity_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().security_group.__dict__["domain_sensitivity"]
    except KeyError:
        pass
