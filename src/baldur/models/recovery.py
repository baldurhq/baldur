"""Recovery Domain Value Types.

OSS-tier value types for recovery-gate configuration and error-budget
deployment overrides. Pure DTOs/enums with no PRO runtime dependency.

This module is intentionally distinct from
:mod:`baldur.models.recovery_session` — the latter holds the
*persistence* model for individual recovery sessions (with a
session-lifecycle ``RecoveryStatus``), while this module holds
*configuration* and *operator-action* types that surround the
recovery flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from baldur.core.serializable import SerializableMixin


class OverrideType(str, Enum):
    """Reason classification for deployment-freeze overrides."""

    HOTFIX = "hotfix"
    SECURITY_PATCH = "security_patch"
    EXECUTIVE_APPROVAL = "executive_approval"
    ROLLBACK = "rollback"


@dataclass
class RecoveryGateConfig(SerializableMixin):
    """Stabilization-window configuration for safe emergency exit.

    Defines how long the system must remain stable before the recovery
    gate releases emergency mode, plus the metric thresholds that count
    as "stable". Defaults are read from EmergencyModeSettings when
    available.
    """

    stabilization_period_seconds: int = 300
    """Stabilization wait window (seconds)."""

    require_metrics_stable: bool = True
    """Whether metric-based stability checks are required."""

    cpu_threshold_percent: float = 80.0
    """CPU usage must be at or below this to count as stable."""

    error_rate_threshold: float = 0.05
    """Error rate must be at or below this (5%) to count as stable."""

    gradual_recovery: bool = True
    """Whether to step down emergency level gradually."""

    level_step_delay_seconds: int = 60
    """Delay between level-down steps (seconds)."""

    health_check_interval_seconds: int = 30
    """Metric re-check cadence during recovery (seconds)."""

    auto_rollback_on_failure: bool = True
    """Whether to roll back automatically when recovery fails."""

    @classmethod
    def from_settings(cls) -> RecoveryGateConfig:
        """Create from EmergencyModeSettings, with hardcoded fallback."""
        try:
            from baldur.settings.emergency_mode import get_emergency_mode_settings

            s = get_emergency_mode_settings()
            return cls(
                stabilization_period_seconds=s.stabilization_period_seconds,
                cpu_threshold_percent=s.cpu_threshold_percent,
                error_rate_threshold=s.error_rate_threshold,
                level_step_delay_seconds=s.level_step_delay_seconds,
                health_check_interval_seconds=s.health_check_interval_seconds,
            )
        except Exception:
            return cls()


__all__ = ["OverrideType", "RecoveryGateConfig"]
