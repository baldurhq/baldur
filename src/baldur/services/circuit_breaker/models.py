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
        recovery_strategy: Per-service Recovery strategy override
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

    # Per-service Recovery strategy override
    recovery_strategy: RecoveryStrategy | None = None

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
# Canary Recovery
# =============================================================================


@dataclass(frozen=True)
class CanaryRecoveryStageConfig:
    """
    Individual Canary stage.

    Immutable: stage parameters cannot be edited on a handed-out
    strategy; build a new stage tuple instead.

    Attributes:
        traffic_percent: Allowed traffic ratio (0~100)
        duration_seconds: How long this stage lasts (0=advance immediately)
        required_success_rate: Success rate required to advance to the next stage
        description: stage description
    """

    traffic_percent: float  # Allowed traffic ratio (0~100)
    duration_seconds: int  # How long this stage lasts (0=advance immediately)
    required_success_rate: float  # Success rate required to advance to the next stage
    description: str = ""  # stage description

    def __post_init__(self) -> None:
        """Validate canary stage values."""
        if not (0.0 <= self.traffic_percent <= 100.0):
            raise ValueError(
                f"traffic_percent must be between 0 and 100, got {self.traffic_percent}"
            )
        if self.duration_seconds < 0:
            raise ValueError(
                f"duration_seconds must be non-negative, got {self.duration_seconds}"
            )
        if not (0.0 <= self.required_success_rate <= 100.0):
            raise ValueError(
                f"required_success_rate must be between 0 and 100, "
                f"got {self.required_success_rate}"
            )


@dataclass(frozen=True)
class RecoveryStrategy:
    """
    HALF_OPEN → CLOSED recovery strategy.

    Instead of sending 100% traffic immediately in the HALF_OPEN state,
    traffic is increased gradually to prevent a Thundering Herd.

    Immutable (deep): the strategy and its stages cannot be edited on a
    handed-out config; recovery behavior only changes through a fresh
    instance passed to the registration path.

    Attributes:
        type: Strategy type ("immediate" | "canary")
        canary_stages: Canary stage configuration (default 4 stages)
        on_stage_failure: Action on stage failure ("restart" | "abort")
        strict_mode: Strict mode for core services such as payments (requires 100% success rate)
    """

    # Strategy type
    type: str = "canary"  # "immediate" | "canary"

    # Canary stage configuration (default 4 stages); any caller-supplied
    # sequence is coerced to a tuple in __post_init__
    canary_stages: tuple[CanaryRecoveryStageConfig, ...] = field(
        default_factory=lambda: (
            CanaryRecoveryStageConfig(
                traffic_percent=10.0,
                duration_seconds=5,
                required_success_rate=95.0,
                description="Stage 1: observe for 5s at 10% traffic",
            ),
            CanaryRecoveryStageConfig(
                traffic_percent=30.0,
                duration_seconds=5,
                required_success_rate=95.0,
                description="Stage 2: observe for 5s at 30% traffic",
            ),
            CanaryRecoveryStageConfig(
                traffic_percent=60.0,
                duration_seconds=5,
                required_success_rate=90.0,
                description="Stage 3: observe for 5s at 60% traffic",
            ),
            CanaryRecoveryStageConfig(
                traffic_percent=100.0,
                duration_seconds=0,
                required_success_rate=90.0,
                description="Stage 4: full recovery",
            ),
        )
    )

    # Action on stage failure
    on_stage_failure: str = "restart"  # "restart" | "abort"

    # Strict mode for core services such as payments
    strict_mode: bool = False  # If True, every stage requires a 100% success rate

    def __post_init__(self) -> None:
        """Validate recovery strategy values; coerce stages to a tuple."""
        # object.__setattr__ is the frozen-dataclass idiom for
        # __post_init__ normalization (a plain assignment would raise).
        object.__setattr__(self, "canary_stages", tuple(self.canary_stages))

        valid_types = {"immediate", "canary"}
        if self.type not in valid_types:
            raise ValueError(f"Invalid type: {self.type}. Valid values: {valid_types}")

        valid_failure_actions = {"restart", "abort"}
        if self.on_stage_failure not in valid_failure_actions:
            raise ValueError(
                f"Invalid on_stage_failure: {self.on_stage_failure}. "
                f"Valid values: {valid_failure_actions}"
            )


# =============================================================================
# Adaptive Threshold
# =============================================================================


@dataclass
class ThresholdMultiplier:
    """
    Threshold multiplier.

    Attributes:
        failure: Failure-count multiplier
        window: Observation-window multiplier
        description: multiplier description
    """

    failure: float  # Failure-count multiplier
    window: float  # Observation-window multiplier
    description: str = ""

    def __post_init__(self) -> None:
        """Validate multiplier values."""
        if self.failure < 0:
            raise ValueError(
                f"failure multiplier must be non-negative, got {self.failure}"
            )
        if self.window < 0:
            raise ValueError(
                f"window multiplier must be non-negative, got {self.window}"
            )


@dataclass
class AdaptiveThresholdPolicy:
    """
    Automatic CB threshold adjustment by Emergency Level.

    Adjusts CB thresholds automatically according to the system Emergency Level.
    The more severe the situation, the more conservative (looser) the setting,
    to prevent a self-induced blackout.

    Attributes:
        enabled: Whether Adaptive Threshold is enabled
        base_failure_threshold: Base failure-count threshold
        base_window_seconds: Base observation window
        level_multipliers: Per-Emergency-Level multipliers
    """

    enabled: bool = True

    # Defaults
    base_failure_threshold: int = 5  # Base failure-count threshold
    base_window_seconds: int = 60  # Base observation window

    # Per-Emergency-Level multipliers
    level_multipliers: dict[str, ThresholdMultiplier] = field(
        default_factory=lambda: {
            "NORMAL": ThresholdMultiplier(
                failure=1.0, window=1.0, description="Normal: 5 failures / 60s"
            ),
            "ELEVATED": ThresholdMultiplier(
                failure=1.5, window=1.5, description="Elevated: 7.5 failures / 90s"
            ),
            "HIGH": ThresholdMultiplier(
                failure=2.0, window=2.0, description="Warning: 10 failures / 120s"
            ),
            "CRITICAL": ThresholdMultiplier(
                failure=3.0, window=3.0, description="Critical: 15 failures / 180s"
            ),
            "LOCKDOWN": ThresholdMultiplier(
                failure=float("inf"),  # effectively forbids OPEN
                window=float("inf"),
                description="Lockdown: auto-OPEN forbidden",
            ),
        }
    )

    def get_adjusted_threshold(self, emergency_level: str) -> tuple[float, float]:
        """
        Return the adjusted thresholds for the given Emergency Level.

        Args:
            emergency_level: Current Emergency Level

        Returns:
            tuple[float, float]: (adjusted failure threshold, adjusted window seconds)
        """
        multiplier = self.level_multipliers.get(
            emergency_level, self.level_multipliers["NORMAL"]
        )
        return (
            self.base_failure_threshold * multiplier.failure,
            self.base_window_seconds * multiplier.window,
        )


# =============================================================================
# Open Strategy
# =============================================================================


@dataclass
class OpenStrategy:
    """
    CB OPEN strategy.

    Attributes:
        type: Strategy type ("immediate" | "graceful")
        drain_timeout_seconds: Max wait for in-flight requests (Graceful only)
        force_after_timeout: Force OPEN after timeout (Graceful only)

    Note:
        The Delayed strategy (block after N seconds) is an anti-pattern and is not supported.
        - Keeps sending requests to a failing server for N seconds → thread occupation, connection-pool exhaustion
        - A primary cause of Cascading Failure
    """

    type: str = "immediate"  # "immediate" | "graceful"

    # Graceful-only settings
    drain_timeout_seconds: int = 30  # Max wait for in-flight requests

    # Fallback when Graceful fails
    force_after_timeout: bool = True  # Force OPEN after timeout

    def __post_init__(self) -> None:
        """Validate open strategy values."""
        valid_types = {"immediate", "graceful"}
        if self.type not in valid_types:
            raise ValueError(f"Invalid type: {self.type}. Valid values: {valid_types}")
        if self.drain_timeout_seconds < 0:
            raise ValueError(
                f"drain_timeout_seconds must be non-negative, "
                f"got {self.drain_timeout_seconds}"
            )


# =============================================================================
# Integrated Configuration
# =============================================================================


@dataclass
class CircuitBreakerAdvancedConfig:
    """
    Circuit Breaker advanced protection configuration.

    Manages the integrated configuration of all advanced protection features.

    Attributes:
        services: Registered service list (required user setting)
        load_shedding: Load Shedding policy
        adaptive_threshold: Adaptive Threshold policy
        default_recovery: Default Recovery strategy
        default_open_strategy: Default Open strategy
        blast_radius_integration: Enable Blast Radius integration
        blast_radius_block_on_critical: Block auto-OPEN on CRITICAL
        freeze_on_lockdown: Activate Freeze Mode on LOCKDOWN
        allow_manual_override_in_lockdown: Allow manual operations during LOCKDOWN
    """

    # Service registration (required user setting)
    services: list[ServiceConfig] = field(default_factory=list)

    # Load Shedding policy
    load_shedding: LoadSheddingPolicy = field(default_factory=LoadSheddingPolicy)

    # Adaptive Threshold policy
    adaptive_threshold: AdaptiveThresholdPolicy = field(
        default_factory=AdaptiveThresholdPolicy
    )

    # Default Recovery strategy
    default_recovery: RecoveryStrategy = field(default_factory=RecoveryStrategy)

    # Default Open strategy
    default_open_strategy: OpenStrategy = field(default_factory=OpenStrategy)

    # Blast Radius integration
    blast_radius_integration: bool = True
    blast_radius_block_on_critical: bool = True

    # Freeze Mode settings
    freeze_on_lockdown: bool = True
    allow_manual_override_in_lockdown: bool = True

    def get_service_config(self, service_id: str) -> ServiceConfig | None:
        """
        Look up configuration by service ID.

        Args:
            service_id: Service ID

        Returns:
            ServiceConfig or None if not found
        """
        for service in self.services:
            if service.service_id == service_id:
                return service
        return None

    def get_services_by_criticality(self, criticality: str) -> list[ServiceConfig]:
        """
        Look up the service list by criticality.

        Args:
            criticality: Importance level

        Returns:
            Services with the given criticality
        """
        return [s for s in self.services if s.criticality == criticality]

    def get_shedding_targets(self, shed_criticality: list[str]) -> list[ServiceConfig]:
        """
        Look up the list of Load Shedding target services.

        Args:
            shed_criticality: list of criticality levels to shed

        Returns:
            Target services to shed (sorted by shed_priority)
        """
        targets = [
            s
            for s in self.services
            if s.criticality in shed_criticality and s.shed_priority > 0
        ]
        return sorted(targets, key=lambda s: s.shed_priority, reverse=True)


# =============================================================================
# Panic Threshold Configuration
# =============================================================================


@dataclass
class PanicThresholdConfig:
    """
    Panic Threshold configuration.

    When 70% or more of all CBs are OPEN, the system is considered to be in
    total collapse and auto-OPEN is forbidden.

    Attributes:
        enabled: Whether Panic Threshold is enabled
        threshold_percent: OPEN-CB ratio threshold (default 70%)
        action: Action when threshold is exceeded ("freeze" | "alert_only")
    """

    enabled: bool = True
    threshold_percent: float = 70.0  # Panic when 70% or more are OPEN
    action: str = "freeze"  # "freeze" | "alert_only"

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
