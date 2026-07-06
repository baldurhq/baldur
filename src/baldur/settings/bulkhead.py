"""
Bulkhead Settings - Pydantic v2.

Per-domain resource isolation settings.
Supports defaults per ConnectionType and custom domain settings.

Environment Variables:
    BALDUR_BULKHEAD_DATABASE_MAX_CONCURRENT=10
    BALDUR_BULKHEAD_CACHE_MAX_CONCURRENT=20
    BALDUR_BULKHEAD_EXTERNAL_API_MAX_WORKERS=5
    BALDUR_BULKHEAD_EXTERNAL_API_QUEUE_SIZE=10
    BALDUR_BULKHEAD_MESSAGE_QUEUE_MAX_CONCURRENT=15
    BALDUR_BULKHEAD_DEFAULT_MAX_CONCURRENT=10
    BALDUR_BULKHEAD_METRICS_ENABLED=true
    BALDUR_BULKHEAD_METRICS_UPDATE_INTERVAL=10.0
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import MediumCount


class BulkheadSettings(BaseSettings):
    """Bulkhead pattern settings."""

    model_config = make_settings_config("BALDUR_BULKHEAD_")

    # ==========================================================================
    # Per-ConnectionType settings
    # ==========================================================================
    database_max_concurrent: MediumCount = Field(
        default=10,
        description="Maximum concurrent executions for DATABASE type",
    )

    cache_max_concurrent: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Maximum concurrent executions for CACHE type",
    )

    external_api_max_workers: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Thread pool worker count for EXTERNAL_API type",
    )

    external_api_queue_size: int = Field(
        default=10,
        ge=0,
        le=100,
        description="Queue size for EXTERNAL_API type",
    )

    message_queue_max_concurrent: MediumCount = Field(
        default=15,
        description="Maximum concurrent executions for MESSAGE_QUEUE type",
    )

    # ==========================================================================
    # Prometheus metrics updater (PRO) — gates the BulkheadMetricsUpdater that
    # baldur.init() auto-starts on every framework via the startup-integration
    # slot. The interval doubles as the DaemonWorkerHandle staleness tick.
    # ==========================================================================
    metrics_enabled: bool = Field(
        default=True,
        description="Enable the bulkhead Prometheus metrics updater thread",
    )

    metrics_update_interval: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        description="Bulkhead metrics updater refresh interval (seconds)",
    )

    # ==========================================================================
    # Default for custom domains
    # ==========================================================================
    default_max_concurrent: MediumCount = Field(
        default=10,
        description="Default maximum concurrent executions for custom domains",
    )

    # ==========================================================================
    # Multi-instance support (per DB alias, cache instance)
    # ==========================================================================
    database_aliases: dict[str, int] = Field(
        default_factory=lambda: {
            "default": 10,
            "replica": 15,  # replica is read-only, so allow more concurrency
        },
        description="Max concurrent settings per DB alias",
    )

    cache_instances: dict[str, int] = Field(
        default_factory=lambda: {
            "default": 20,
            "session": 10,
        },
        description="Max concurrent settings per cache instance",
    )


def get_bulkhead_settings() -> BulkheadSettings:
    from baldur.settings.root import get_config

    return get_config().resilience.bulkhead


def reset_bulkhead_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().resilience.__dict__["bulkhead"]
    except KeyError:
        pass
