"""
Health Check Settings - Pydantic v2.

Health check infrastructure configuration for adapters/health_checker.py
and meta/health_probe.py thresholds.

Domain-specific health settings (Cell Topology, Propagation, etc.) are
placed in their respective domain settings to avoid God Object anti-pattern.

Environment Variables:
    BALDUR_HEALTH_CHECK_CHECKER_CACHE_TTL_SECONDS=5.0
    BALDUR_HEALTH_CHECK_TCP_INFO_TIMEOUT_SECONDS=0.1
    BALDUR_HEALTH_CHECK_SOCKET_TIMEOUT_SECONDS=1.0
    BALDUR_HEALTH_CHECK_PROBE_CB_OPEN_THRESHOLD=3
    BALDUR_HEALTH_CHECK_PROBE_ACTIVE_RECOVERIES_THRESHOLD=10
    BALDUR_HEALTH_CHECK_PROBE_MEMORY_USAGE_THRESHOLD=0.8
    BALDUR_HEALTH_CHECK_PROBE_WORKER_JOIN_TIMEOUT=2.0

Reference:
- docs/baldur/middleware_system/339_SETTINGS_GAP_HEALTH_SHUTDOWN_CONTROL.md
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import MediumCount


class HealthCheckSettings(BaseSettings):
    """
    Health check infrastructure settings.

    Covers:
    - adapters/health_checker.py: TTL cache and timeout defaults
    - meta/health_probe.py: probe thresholds for DEGRADED status
    """

    model_config = make_settings_config("BALDUR_HEALTH_CHECK_")

    # =========================================================================
    # Health Checker Adapter (adapters/health_checker.py)
    # =========================================================================
    checker_cache_ttl_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=60.0,
        description="TTLCacheStrategy default cache TTL (seconds).",
    )
    tcp_info_timeout_seconds: float = Field(
        default=0.1,
        ge=0.01,
        le=5.0,
        description="LinuxTCPInfoStrategy connection timeout (seconds).",
    )
    socket_timeout_seconds: float = Field(
        default=1.0,
        ge=0.1,
        le=30.0,
        description="SimpleSocketStrategy connection timeout (seconds).",
    )

    # =========================================================================
    # Meta Health Probe (meta/health_probe.py)
    # =========================================================================
    probe_cb_open_threshold: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Circuit Breaker OPEN count threshold for DEGRADED status.",
    )
    probe_active_recoveries_threshold: MediumCount = Field(
        default=10,
        description="Active recoveries count threshold for DEGRADED status.",
    )
    probe_memory_usage_threshold: float = Field(
        default=0.8,
        ge=0.1,
        le=1.0,
        description="Redis memory usage ratio threshold for DEGRADED status.",
    )
    probe_worker_join_timeout: float = Field(
        default=2.0,
        ge=0.5,
        le=30.0,
        description="Worker thread join timeout (seconds).",
    )


def get_health_check_settings() -> "HealthCheckSettings":
    """Return cached HealthCheckSettings via RootConfig."""
    from baldur.settings.root import get_config

    return get_config().core.health_check


def reset_health_check_settings() -> None:
    """Reset cached HealthCheckSettings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["health_check"]
    except KeyError:
        pass
