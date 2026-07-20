"""
Air-Gap Settings - Pydantic v2.

Air-Gap store settings.
Acts as the intermediate store between the business DB and the Baldur engine.

Replaces:
- adapters/airgap/redis_adapter.py:DEFAULT_TTL

Environment Variables:
    BALDUR_AIRGAP_REDIS_TTL=3600
    BALDUR_AIRGAP_KEY_PREFIX=sh:airgap:

Usage:
    from baldur.settings.airgap import get_airgap_settings
    settings = get_airgap_settings()
    ttl = settings.redis_ttl
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class AirGapSettings(BaseSettings):
    """
    Air-Gap store settings.

    Air-Gap is the isolation layer between the business layer and the Baldur
    engine. A business DB change writes a summary state to Redis, and the
    Baldur engine reads state only from Redis.

    Attributes:
        redis_ttl: TTL of the Air-Gap state stored in Redis (seconds)
        key_prefix: Redis key prefix
    """

    model_config = make_settings_config("BALDUR_AIRGAP_")

    # ==========================================================================
    # Redis TTL - from adapters/airgap/redis_adapter.py
    # ==========================================================================
    redis_ttl: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="TTL for Air-Gap state stored in Redis (seconds). Default 1 hour.",
    )

    # ==========================================================================
    # Key Prefix
    # ==========================================================================
    key_prefix: str = Field(
        default="sh:airgap:",
        description="Redis key prefix",
    )

    @field_validator("key_prefix")
    @classmethod
    def validate_key_prefix(cls, v: str) -> str:
        """Ensure the key prefix ends with a colon."""
        if not v.endswith(":"):
            return f"{v}:"
        return v


def get_airgap_settings() -> "AirGapSettings":
    from baldur.settings.root import get_config

    return get_config().testing.airgap


def reset_airgap_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().testing.__dict__["airgap"]
    except KeyError:
        pass
