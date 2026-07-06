"""
Metrics Settings - Pydantic v2.

Single Source of Truth for metrics collection configuration.

Replaces:
- core/config.py:MetricsConfig (lines 241-250)
- core/safe_defaults.py:SAFE_DEFAULTS["metrics"]
- core/safe_defaults.py:VALIDATION_RULES["metrics"]
- config.py:MetricCollectionSettings (lines 418-517)

Environment Variables:
    BALDUR_METRICS_ENABLED=true
    BALDUR_METRICS_ENABLED=true

Reference:
- docs/baldur/middleware_system/40_PYDANTIC_CONFIG_MIGRATION.md
- docs/baldur/middleware_system/358_LARGE_SERVICE_IMPROVEMENT.md
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class MetricsSettings(BaseSettings):
    """
    Metrics collection configuration with validation.

    All defaults match core/config.py:MetricsConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["metrics"]
    """

    model_config = make_settings_config("BALDUR_METRICS_")

    # ==========================================================================
    # Core Settings (from core/config.py lines 243-247)
    # Validation rules from core/safe_defaults.py lines 284-286
    # ==========================================================================
    enabled: bool = Field(
        default=True,
        description="Enable metrics collection",
    )
    prefix: str = Field(
        default="baldur",
        description="Prefix for all metrics names",
    )
    # ==========================================================================
    # Jitter Settings (Thundering Herd prevention)
    # From core/config.py lines 249-250
    # ==========================================================================
    jitter_enabled: bool = Field(
        default=True,
        description="Enable jitter for collection intervals",
    )
    jitter_max_delay_seconds: float = Field(
        default=60.0,
        ge=0.0,
        le=300.0,
        description="Maximum jitter delay in seconds",
    )

    # ==========================================================================
    # Snapshot Storage - from metrics/snapshot_storage.py
    # ==========================================================================
    snapshot_max_age: int = Field(
        default=3600,
        ge=300,
        le=86400,
        description="Metric snapshot max age (seconds). Default 1 hour.",
    )

    # ==========================================================================
    # Cardinality Guard (332 Metric Cardinality Guard)
    # ==========================================================================
    max_distinct_endpoints: int = Field(
        default=500,
        ge=50,
        le=5000,
        description="Max distinct normalized endpoints. Oldest evicted via LRU when exceeded.",
    )
    max_registered_domains: int = Field(
        default=50,
        ge=10,
        le=500,
        description="Max registered domain count for metric labels.",
    )
    endpoint_cache_size: int = Field(
        default=2048,
        ge=256,
        le=65536,
        description="Path-to-normalized endpoint cache size. LRU eviction.",
    )

    # ==========================================================================
    # Snapshot Storage (368: Django Settings Decoupling)
    # ==========================================================================
    snapshot_dir: str | None = Field(
        default=None,
        description="Metric snapshot storage directory. Falls back to <CWD>/.baldur if None",
    )

    # ==========================================================================
    # Adapter & Storage (from config.py:MetricCollectionSettings)
    # ==========================================================================
    adapter_type: str | None = Field(
        default="null",
        description="Metrics adapter type (null, redis, etc.)",
    )
    redis_prefix: str = Field(
        default="sh:metrics:",
        description="Redis key prefix for metrics storage",
    )

    # ==========================================================================
    # Drift Detection (from config.py:MetricCollectionSettings)
    # ==========================================================================
    drift_warning_threshold: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Drift warning threshold (5%)",
    )
    drift_critical_threshold: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description="Drift critical threshold (20%)",
    )
    drift_incident_threshold: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description="Drift incident threshold (50%)",
    )


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_metrics_settings() -> "MetricsSettings":
    from baldur.settings.root import get_config

    return get_config().metrics_group.metrics


def reset_metrics_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().metrics_group.__dict__["metrics"]
    except KeyError:
        pass


def get_metric_collection_settings_safe() -> "MetricsSettings":
    """Get metrics settings with environment variable drift detection."""
    from baldur.settings.drift_monitor import get_config_drift_monitor

    monitor = get_config_drift_monitor()
    if monitor.check_and_invalidate("metric_collection", "BALDUR_METRICS_"):
        reset_metrics_settings()
    return get_metrics_settings()
