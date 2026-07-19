"""
Cascade audit configuration.

Defines the configuration values used when processing cascade events.

Settings:
- CascadeChainConfig: chain depth limits
- AuditBackpressureConfig: backpressure settings
"""

from __future__ import annotations

from dataclasses import dataclass

# =============================================================================
# CascadeChainConfig
# =============================================================================


@dataclass
class CascadeChainConfig:
    """
    Cascade chain depth settings.

    Limits chain depth so that automated systems cannot trigger an
    excessive chain reaction between one another.

    Attributes:
        max_chain_depth: maximum chain depth (warn/block when exceeded)
        warn_at_depth: depth at which a warning is emitted
        block_on_exceed: whether to block once the depth is exceeded
        detect_cycles: enable cycle detection
    """

    max_chain_depth: int = 10
    """
    Maximum chain depth.

    Exceeding this value emits a warning or blocks.
    The default of 10 covers most healthy cases.
    """

    warn_at_depth: int = 7
    """
    Depth at which a warning is emitted.

    Reaching this depth logs a warning.
    """

    block_on_exceed: bool = True
    """
    Whether to block once the depth is exceeded.

    True: raise CascadeChainDepthExceeded
    False: warn only and continue
    """

    detect_cycles: bool = True
    """
    Enable cycle detection.

    True: raise CascadeCycleDetected when a cycle is found
    False: disable cycle detection
    """

    def __post_init__(self) -> None:
        """Validate the configured values."""
        if self.warn_at_depth >= self.max_chain_depth:
            # warn_at_depth must stay below max_chain_depth
            self.warn_at_depth = max(1, self.max_chain_depth - 3)


# =============================================================================
# Default Configurations
# =============================================================================


DEFAULT_CASCADE_CHAIN_CONFIG = CascadeChainConfig()
"""Default cascade chain configuration."""


def get_cascade_chain_config() -> CascadeChainConfig:
    """
    Return the cascade chain configuration.

    Loads the values through CascadeSettings (Pydantic).
    """
    try:
        from baldur.settings.cascade import get_cascade_settings

        s = get_cascade_settings()
        return CascadeChainConfig(
            max_chain_depth=s.max_depth,
            warn_at_depth=s.warn_depth,
            block_on_exceed=s.block_on_exceed,
            detect_cycles=s.detect_cycles,
        )
    except Exception:
        return DEFAULT_CASCADE_CHAIN_CONFIG


# =============================================================================
# AuditBackpressureConfig (Phase 5)
# =============================================================================


@dataclass
class AuditBackpressureConfig:
    """
    Audit backpressure settings.

    Applies load shedding so that a high-load audit system cannot escalate
    into a system-wide outage.

    Attributes:
        load_shedding_enabled: whether load shedding is enabled
        buffer_warning_threshold: buffer warning threshold (default 0.7 = 70%)
        buffer_critical_threshold: buffer critical threshold (default 0.9 = 90%)
        max_events_per_second: maximum events processed per second
        fallback_enabled: whether the local fallback is enabled
        metrics_enabled: whether metrics recording is enabled
    """

    load_shedding_enabled: bool = True
    """Whether load shedding is enabled."""

    buffer_warning_threshold: float = 0.7
    """
    Buffer warning threshold (0.0 ~ 1.0).

    Above this ratio, LOW priority events start being dropped.
    """

    buffer_critical_threshold: float = 0.9
    """
    Buffer critical threshold (0.0 ~ 1.0).

    Above this ratio, MEDIUM priority events are dropped as well.
    """

    max_events_per_second: int = 1000
    """
    Maximum events processed per second.

    Load shedding applies above this rate.
    """

    fallback_enabled: bool = True
    """Whether the local fallback is enabled."""

    metrics_enabled: bool = True
    """Whether metrics recording is enabled."""

    def __post_init__(self) -> None:
        """Validate the configured values."""
        if not 0.0 <= self.buffer_warning_threshold <= 1.0:
            self.buffer_warning_threshold = 0.7
        if not 0.0 <= self.buffer_critical_threshold <= 1.0:
            self.buffer_critical_threshold = 0.9
        if self.buffer_warning_threshold >= self.buffer_critical_threshold:
            self.buffer_warning_threshold = self.buffer_critical_threshold - 0.2


DEFAULT_BACKPRESSURE_CONFIG = AuditBackpressureConfig()
"""Default backpressure configuration."""


def get_audit_backpressure_config() -> AuditBackpressureConfig:
    """
    Return the audit backpressure configuration.

    Loads the values through AuditSettings (Pydantic).
    """
    try:
        from baldur.settings.audit import get_audit_settings

        s = get_audit_settings()
        return AuditBackpressureConfig(
            load_shedding_enabled=s.load_shedding_enabled,
            buffer_warning_threshold=s.buffer_warning_threshold,
            buffer_critical_threshold=s.buffer_critical_threshold,
            max_events_per_second=s.max_events_per_second,
            fallback_enabled=s.fallback_enabled,
            metrics_enabled=s.metrics_enabled,
        )
    except Exception:
        return DEFAULT_BACKPRESSURE_CONFIG
