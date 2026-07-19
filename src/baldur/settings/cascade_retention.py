"""
Cascade Retention Settings - Pydantic v2.

Retention and load-shedding settings for cascade audit events.

Cascade events are held in a single tier backed by the configured state
backend. Retention is expressed as a TTL on each event write, which the
Redis backend honors and the file backend ignores; the event index is
additionally capped by size. There is no automatic movement of events to
any secondary store.

Environment Variables:
    BALDUR_CASCADE_RETENTION_HOT_RETENTION_DAYS=7
    BALDUR_CASCADE_RETENTION_MAX_CASCADE_INDEX_SIZE=10000
    BALDUR_CASCADE_RETENTION_MAX_EVENTS_PER_SECOND=10000
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class CascadeRetentionSettings(BaseSettings):
    """
    Cascade audit event retention and load-shedding settings.

    Retention:
    - hot_retention_days: TTL applied to every event write. Honored by the
      Redis state backend; the file backend ignores TTLs.
    - max_cascade_index_size: hard cap on the event index, bounding both
      memory use and the number of events reachable by a query.

    Load shedding:
    - buffer_warning_threshold / buffer_critical_threshold: buffer pressure
      tiers that trigger warnings and shedding.
    - max_events_per_second: ingest rate above which sampling kicks in.
    """

    model_config = make_settings_config("BALDUR_CASCADE_RETENTION_")

    # ==========================================================================
    # Retention - from cascade_config.py
    # ==========================================================================
    hot_retention_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="Event TTL (days). Honored by the Redis backend, ignored by the file backend.",
    )

    # ==========================================================================
    # Buffer Settings (from cascade_config.py#L274-281)
    # ==========================================================================
    buffer_warning_threshold: float = Field(
        default=0.7,
        ge=0.5,
        le=0.9,
        description="Buffer usage warning threshold (70%)",
    )

    buffer_critical_threshold: float = Field(
        default=0.9,
        ge=0.7,
        le=0.99,
        description="Buffer usage critical threshold (90%)",
    )

    # ==========================================================================
    # Rate Limiting (from cascade_config.py#L288)
    # ==========================================================================
    max_events_per_second: int = Field(
        default=10000,
        ge=1,
        le=1000000,
        description=(
            "Maximum audit events per second threshold. "
            "100,000+ recommended for enterprise environments. "
            "Sampling or warnings triggered when exceeded."
        ),
    )

    # ==========================================================================
    # Cascade Auditor - from audit/cascade_auditor.py
    # ==========================================================================
    max_cascade_index_size: int = Field(
        default=10000,
        ge=1000,
        le=100000,
        description="Maximum cascade index size. Limits Redis memory usage.",
    )

    @field_validator("buffer_critical_threshold")
    @classmethod
    def validate_buffer_order(cls, v: float, info) -> float:
        """Critical must sit above warning."""
        # Note: cross-field validation belongs in a model_validator, but this
        # only emits a warning.
        if v <= 0.7:
            logger.warning(
                "cascade_retention.buffer_critical_threshold_low",
                setting_value=v,
            )
        return v


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_cascade_retention_settings() -> "CascadeRetentionSettings":
    """Get cached CascadeRetentionSettings instance."""
    from baldur.settings.root import get_config

    return get_config().audit_group.cascade_retention


def reset_cascade_retention_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().audit_group.__dict__["cascade_retention"]
    except KeyError:
        pass
