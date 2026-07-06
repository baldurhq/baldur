"""
Circuit Mesh Settings - Pydantic v2.

Adaptive Circuit Breaker Mesh Coordinator settings.
Dynamic threshold adjustment based on downstream CB state,
damped propagation, sequential recovery, TTL Heartbeat.

Source:
- services/circuit_mesh/mesh_coordinator.py
- services/circuit_mesh/store.py

Environment Variables:
    BALDUR_CIRCUIT_MESH_ENABLED=false
    BALDUR_CIRCUIT_MESH_THRESHOLD_MULTIPLIER=2.0
    BALDUR_CIRCUIT_MESH_RECOVERY_TIMEOUT_MULTIPLIER=3.0
    BALDUR_CIRCUIT_MESH_OVERRIDE_TTL_SECONDS=600
    BALDUR_CIRCUIT_MESH_PROPAGATION_MAX_DEPTH=1
    BALDUR_CIRCUIT_MESH_FAST_RECOVERY_TIMEOUT_SECONDS=5
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    BackoffMultiplier,
    MediumCount,
    Probability,
    ShortInterval,
    ZeroableSmallCount,
)

logger = structlog.get_logger()


class CircuitMeshSettings(BaseSettings):
    """
    Adaptive Circuit Breaker Mesh settings.

    Controls dynamic threshold adjustment on downstream CB OPEN,
    damped propagation, and sequential recovery.
    """

    model_config = make_settings_config("BALDUR_CIRCUIT_MESH_")

    enabled: bool = Field(
        default=False,
        description="Circuit Mesh Coordinator activation toggle",
    )

    # --- Threshold adjustment ---

    threshold_multiplier: BackoffMultiplier = Field(
        default=2.0,
        description="Upstream failure_threshold multiplier on downstream OPEN (2.0 = 2x threshold increase)",
    )

    recovery_timeout_multiplier: BackoffMultiplier = Field(
        default=3.0,
        description="Upstream recovery_timeout multiplier on downstream OPEN (3.0 = 3x timeout extension)",
    )

    override_ttl_seconds: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="Override auto-expiry (seconds) — safety net",
    )

    # --- Damped propagation ---

    propagation_max_depth: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Max propagation depth (1 = direct parent only, 2 = up to grandparent)",
    )

    propagation_damping_factor: Probability = Field(
        default=0.5,
        description="Multiplier damping coefficient per depth (0.5 = 50% applied at depth 2)",
    )

    # --- Sequential recovery ---

    recovery_step_delay_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Wait time between sequential recovery steps (seconds)",
    )

    fast_recovery_timeout_seconds: ShortInterval = Field(
        default=5,
        description="Upstream fast-recovery timeout on downstream recovery (seconds)",
    )

    # --- TTL Heartbeat ---

    max_renewals: ZeroableSmallCount = Field(
        default=3,
        description="Max auto-renewal count (exceed triggers EmergencyCoordinator escalation)",
    )

    renewal_check_threshold_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Start renewal check N seconds before TTL expiry",
    )

    # --- L2 TTL Management ---

    override_l2_ttl_buffer_seconds: int = Field(
        default=120,
        ge=10,
        le=600,
        description=(
            "Buffer added to override remaining TTL for Redis key expiry. "
            "Must exceed snapshot_interval_seconds for periodic check timing safety."
        ),
    )

    # --- Operations ---

    snapshot_interval_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Mesh state snapshot + renewal check interval (seconds, Celery beat)",
    )

    max_concurrent_overrides: MediumCount = Field(
        default=20,
        description="Max concurrent override count (safety guard)",
    )

    # --- Feature Flags ---

    enable_damped_propagation: bool = Field(
        default=False,
        description="Damped propagation activation (False = direct parent override only)",
    )

    enable_preemptive_fallback: bool = Field(
        default=False,
        description="Preemptive Fallback activation (should_allow() pre-check)",
    )

    enable_fast_recovery: bool = Field(
        default=False,
        description="Fast-Recovery override on downstream recovery",
    )

    enable_ttl_heartbeat: bool = Field(
        default=False,
        description="TTL auto-renewal activation",
    )

    # --- Cross-region ---

    cross_region_dependencies: list[str] = Field(
        default_factory=list,
        description="Other region services present in local graph (explicit Opt-in)",
    )

    @field_validator("threshold_multiplier")
    @classmethod
    def validate_threshold_multiplier(cls, v: float) -> float:
        """Warn when threshold_multiplier is 1.0 (no adjustment effect)."""
        if v == 1.0:
            logger.warning(
                "circuit_mesh_settings.threshold_multiplier_is_one",
                setting_value=v,
                hint="multiplier=1.0 means no threshold adjustment",
            )
        return v


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_circuit_mesh_settings() -> "CircuitMeshSettings":
    """
    Return cached CircuitMeshSettings instance.

    Returns:
        CircuitMeshSettings: singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.circuit_mesh


def reset_circuit_mesh_settings() -> None:
    """
    Reset cached settings (for testing).

    Call this function to reload settings after environment variable changes.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["circuit_mesh"]
    except KeyError:
        pass
