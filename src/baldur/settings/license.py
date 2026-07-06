"""
License Settings — Ed25519 subscription validation configuration.

Environment Variables:
    BALDUR_LICENSE_KEY=<base64-encoded signed entitlement>
    BALDUR_LICENSE_FILE=<path to entitlement file>

The module is named ``license`` (not ``entitlement``) so the
``BALDUR_LICENSE_`` env prefix equals the uppercase module name and the
``test_module_prefix_alignment()`` fitness function holds without a
docstring carve-out. The class is still called ``EntitlementSettings``
because the broader subsystem is the entitlement validator (Ed25519
signed token + status enum). License is the file-system convention
that operators interact with (`BALDUR_LICENSE_KEY`, `BALDUR_LICENSE_FILE`
mirror industry tools like `MOSEK_LICENSE_FILE`).
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = [
    "EntitlementSettings",
    "get_entitlement_settings",
    "reset_entitlement_settings",
]


class EntitlementSettings(BaseSettings):
    """Entitlement configuration for PRO subscription validation.

    Two ways to provide the entitlement token:
    - BALDUR_LICENSE_KEY: Base64-encoded signed entitlement (inline)
    - BALDUR_LICENSE_FILE: Path to file containing the entitlement token

    If both are set, ``key`` (BALDUR_LICENSE_KEY) takes precedence.
    Empty = OSS mode (no PRO features).

    The pydantic-settings prefix is ``BALDUR_LICENSE_`` and the fields are
    ``key`` / ``file`` so the resulting env vars stay
    ``BALDUR_LICENSE_KEY`` / ``BALDUR_LICENSE_FILE`` (preserved across the
    508 D2 rename — zero migration for Design Partners).
    """

    model_config = make_settings_config("BALDUR_LICENSE_")

    key: str = Field(
        default="",
        description="Base64-encoded signed entitlement token (BALDUR_LICENSE_KEY)",
    )
    file: str = Field(
        default="",
        description="Path to entitlement token file (BALDUR_LICENSE_FILE)",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================

_instance: EntitlementSettings | None = None


def get_entitlement_settings() -> EntitlementSettings:
    """Return cached EntitlementSettings singleton."""
    global _instance
    if _instance is None:
        _instance = EntitlementSettings()
    return _instance


def reset_entitlement_settings() -> None:
    """Reset cached EntitlementSettings singleton."""
    global _instance
    _instance = None
