"""
Cascade Chain Settings - Pydantic v2.

Cascade chain depth limits and cycle detection configuration.

Replaces:
- audit/cascade_config.py:CascadeChainConfig (Django settings / env var dual path)

Environment Variables:
    BALDUR_CASCADE_MAX_DEPTH=10
    BALDUR_CASCADE_WARN_DEPTH=7
    BALDUR_CASCADE_BLOCK_ON_EXCEED=true
    BALDUR_CASCADE_DETECT_CYCLES=true
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = [
    "CascadeSettings",
    "get_cascade_settings",
    "reset_cascade_settings",
]


class CascadeSettings(BaseSettings):
    """
    Cascade chain depth configuration.

    Prevents runaway cascading reactions between automated healing
    systems by enforcing depth limits and cycle detection.
    """

    model_config = make_settings_config("BALDUR_CASCADE_")

    max_depth: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum cascade chain depth",
    )
    warn_depth: int = Field(
        default=7,
        ge=1,
        le=100,
        description="Warning threshold for chain depth",
    )
    block_on_exceed: bool = Field(
        default=True,
        description="Block execution when depth exceeded",
    )
    detect_cycles: bool = Field(
        default=True,
        description="Enable cycle detection in cascade chains",
    )
    wal_dir: str = Field(
        default="/var/log/baldur/cascade_wal",
        description="Directory for cascade WAL files (local fallback)",
    )

    @model_validator(mode="after")
    def _validate_depth_thresholds(self) -> "CascadeSettings":
        """Ensure warn_depth < max_depth."""
        if self.warn_depth >= self.max_depth:
            raise ValueError(
                f"warn_depth ({self.warn_depth}) "
                f"must be less than max_depth ({self.max_depth})"
            )
        return self


def get_cascade_settings() -> "CascadeSettings":
    """Get cached CascadeSettings instance."""
    from baldur.settings.root import get_config

    return get_config().audit_group.cascade


def reset_cascade_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().audit_group.__dict__["cascade"]
    except KeyError:
        pass
