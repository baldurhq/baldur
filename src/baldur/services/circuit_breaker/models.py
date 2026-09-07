"""
Circuit Breaker Advanced Protection Models

Data model definitions.

This module defines all data models for the Circuit Breaker advanced
protection system.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# =============================================================================
# Service Configuration
# =============================================================================


@dataclass(frozen=True)
class ServiceConfig:
    """
    Service configuration - the user specifies criticality directly.

    Immutable: every field change must route through a fresh instance
    (``dataclasses.replace``) and re-registration, so shedding/recovery
    behavior can only change via the validated, logged registration
    path — never by mutating a handed-out config object.

    Attributes:
        service_id: Unique service identifier
        criticality: Importance level ("critical" | "high" | "medium" | "low")
        shed_priority: Load Shedding priority (higher sheds first, 0=never shed)
        min_traffic_percentage: Minimum guaranteed traffic (0~100%)
        failure_threshold: Per-service CB failure threshold override
        window_seconds: Per-service CB observation window override

    Example:
        >>> config = ServiceConfig(
        ...     service_id="payment-api",
        ...     criticality="critical",
        ...     shed_priority=0,  # never shed
        ... )
    """

    service_id: str

    # Criticality level (must be user-specified)
    criticality: str  # "critical" | "high" | "medium" | "low"

    # Load Shedding priority (higher sheds first, 0=never shed)
    shed_priority: int = 0

    # Minimum guaranteed traffic (0~100%)
    min_traffic_percentage: float = 5.0

    # Per-service CB config override
    failure_threshold: int | None = None
    window_seconds: int | None = None

    def __post_init__(self) -> None:
        """Validate criticality value."""
        valid_levels = {"critical", "high", "medium", "low"}
        if self.criticality not in valid_levels:
            raise ValueError(
                f"Invalid criticality: {self.criticality}. Valid values: {valid_levels}"
            )
        if not (0.0 <= self.min_traffic_percentage <= 100.0):
            raise ValueError(
                f"min_traffic_percentage must be between 0 and 100, "
                f"got {self.min_traffic_percentage}"
            )
        if self.shed_priority < 0:
            raise ValueError(
                f"shed_priority must be non-negative, got {self.shed_priority}"
            )


# =============================================================================
# Load Shedding
# =============================================================================


@dataclass
class SheddingLevel:
    """
    Individual Shedding level.

    Attributes:
        error_rate: critical service error-rate threshold
        shed_criticality: list of criticality levels to shed
        traffic_limit: allowed traffic % (0=fully blocked, 100=no limit)
        description: level description
    """

    error_rate: float  # critical service error-rate threshold
    shed_criticality: list[str]  # list of criticality levels to shed
    traffic_limit: float  # allowed traffic % (0=fully blocked, 100=no limit)
    description: str = ""  # level description

    def __post_init__(self) -> None:
        """Validate shedding level values."""
        if not (0.0 <= self.error_rate <= 100.0):
            raise ValueError(
                f"error_rate must be between 0 and 100, got {self.error_rate}"
            )
        if not (0.0 <= self.traffic_limit <= 100.0):
            raise ValueError(
                f"traffic_limit must be between 0 and 100, got {self.traffic_limit}"
            )
        # critical can never be a shedding target
        if "critical" in self.shed_criticality:
            raise ValueError("'critical' cannot be included in shed_criticality")


@dataclass
class LoadSheddingPolicy:
    """
    Load Shedding policy.

    When core services show signs of failure, traffic to non-core services is
    limited first to concentrate resources on the core services.

    Attributes:
        enabled: Whether Load Shedding is enabled
        trigger_threshold: Start shedding when a critical service's error rate exceeds this
        levels: Per-level shedding policy (default 3 levels, extensible)
    """

    enabled: bool = True

    # Trigger condition: start shedding when a critical service's error rate exceeds this
    trigger_threshold: float = 30.0

    # Per-level shedding policy (default 3 levels, extensible)
    levels: list[SheddingLevel] = field(
        default_factory=lambda: [
            SheddingLevel(
                error_rate=30.0,
                shed_criticality=["low"],
                traffic_limit=50.0,
                description="Level 1: low criticality 50% restricted",
            ),
            SheddingLevel(
                error_rate=50.0,
                shed_criticality=["low", "medium"],
                traffic_limit=20.0,
                description="Level 2: low+medium 80% restricted",
            ),
            SheddingLevel(
                error_rate=70.0,
                shed_criticality=["low", "medium"],
                traffic_limit=0.0,
                description="Level 3: low+medium fully blocked",
            ),
        ]
    )


# =============================================================================
# Panic Threshold Configuration
# =============================================================================


# Consecutive triggered ticks required before the monitor escalates. Two
# ticks of hysteresis keep a single sampling artefact from declaring a
# fleet-wide collapse; the periodic lane's interval sets what that costs in
# detection latency.
DEFAULT_CONSECUTIVE_TRIGGERS_REQUIRED = 2

# Smallest fleet whose OPEN ratio is meaningful. Below it a single OPEN
# breaker clears any percentage threshold, so the ratio says nothing about
# the system.
DEFAULT_MIN_REGISTERED_SERVICES = 3


@dataclass
class PanicThresholdConfig:
    """
    Panic Threshold configuration.

    When 70% or more of all CBs are OPEN, the system is considered to be in
    total collapse and Emergency Level 3 is declared.

    Attributes:
        threshold_percent: OPEN-CB ratio threshold (default 70%)
        action: Action when threshold is exceeded ("freeze" | "alert_only")
        consecutive_triggers_required: Triggered ticks the escalation lane
            waits for before declaring; the instantaneous probe ignores it
        min_registered_services: Fleet size below which the ratio is not
            judged at all
    """

    threshold_percent: float = 70.0  # Panic when 70% or more are OPEN
    action: str = "freeze"  # "freeze" | "alert_only"
    consecutive_triggers_required: int = DEFAULT_CONSECUTIVE_TRIGGERS_REQUIRED
    min_registered_services: int = DEFAULT_MIN_REGISTERED_SERVICES

    def __post_init__(self) -> None:
        """Validate panic threshold values."""
        if not (0.0 <= self.threshold_percent <= 100.0):
            raise ValueError(
                f"threshold_percent must be between 0 and 100, "
                f"got {self.threshold_percent}"
            )
        valid_actions = {"freeze", "alert_only"}
        if self.action not in valid_actions:
            raise ValueError(
                f"Invalid action: {self.action}. Valid values: {valid_actions}"
            )
        if self.consecutive_triggers_required < 1:
            raise ValueError(
                f"consecutive_triggers_required must be >= 1, "
                f"got {self.consecutive_triggers_required}"
            )
        if self.min_registered_services < 1:
            raise ValueError(
                f"min_registered_services must be >= 1, "
                f"got {self.min_registered_services}"
            )


# =============================================================================
# Freeze Mode State
# =============================================================================


@dataclass
class FreezeModeState:
    """
    Freeze Mode state.

    Freezes the current CB states as-is in the LOCKDOWN state.

    Attributes:
        active: Whether Freeze Mode is active
        activated_at: Activation time (ISO format)
        reason: Activation reason
        activated_by: Who activated it ("system" | "operator:username")
    """

    active: bool = False
    activated_at: str | None = None  # ISO format timestamp
    reason: str = ""
    activated_by: str = ""  # "system" or "operator:<username>"
