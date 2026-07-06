"""
Emergency Mode metric recorder — metric definitions and recording.

Owns all Emergency Mode-related Prometheus metrics.
See DD-5 for SRE-Core scope rationale.

Note: Drift detection metrics (record_emergency_cache_drift, etc.) live in
metrics/drift_metrics.py and track cache state — no overlap with this
recorder which covers mode state transitions and recovery lifecycle.
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
    get_or_create_histogram,
)

logger = structlog.get_logger()

__all__ = [
    "EmergencyModeMetricRecorder",
    "set_em_level",
    "set_em_active",
    "record_em_activation",
    "record_em_duration",
    "set_em_recovery_active",
    "record_em_recovery_step",
    "record_em_recovery_rollback",
]

_LEVEL_MAP = {"normal": 0, "level_1": 1, "level_2": 2, "level_3": 3}


class EmergencyModeMetricRecorder(BaseMetricRecorder):
    """Emergency Mode metric definitions and recording (7 methods).

    DD-6: String interface — level mapped to int internally.
    """

    def __init__(self) -> None:
        self._level = get_or_create_gauge(
            f"{self.PREFIX}_emergency_mode_level",
            "Current emergency level (0-3)",
            [],
        )
        self._active = get_or_create_gauge(
            f"{self.PREFIX}_emergency_mode_active",
            "1=active, 0=inactive",
            [],
        )
        self._activations_total = get_or_create_counter(
            f"{self.PREFIX}_emergency_mode_activations_total",
            "Activation count",
            ["level", "trigger_type", "is_synthetic"],
        )
        self._duration = get_or_create_histogram(
            f"{self.PREFIX}_emergency_mode_duration_seconds",
            "Duration of emergency mode activation",
            ["level"],
            buckets=(60, 300, 600, 1800, 3600, 7200, 14400, 86400),
        )
        self._recovery_active = get_or_create_gauge(
            f"{self.PREFIX}_emergency_mode_recovery_active",
            "1=gradual recovery in progress, 0=not",
            [],
        )
        self._recovery_steps_total = get_or_create_counter(
            f"{self.PREFIX}_emergency_mode_recovery_steps_total",
            "Level step-down completions during gradual recovery",
            ["from_level", "to_level"],
        )
        self._recovery_rollbacks_total = get_or_create_counter(
            f"{self.PREFIX}_emergency_mode_recovery_rollbacks_total",
            "Gradual recovery rollbacks (metrics check failed)",
            [],
        )

    def set_level(self, level: str) -> None:
        """Set current emergency level gauge.

        Maps level string to int via _LEVEL_MAP.
        """
        try:
            # No str() coercion: EmergencyLevel is a (str, Enum), so members
            # hash and compare by value and hit the map directly — while
            # str(member) returns the member path ('EmergencyLevel.LEVEL_2'),
            # which always misses. Unhashable input raises TypeError into the
            # fail-open except below.
            value = _LEVEL_MAP.get(level)
            if value is None:
                logger.warning(
                    "metrics.set_emergency_level_failed",
                    reason="unmapped_value",
                    level=repr(level),
                )
                value = 0
            self._level.set(value)
        except Exception as e:
            logger.warning("metrics.set_emergency_level_failed", error=e)

    def set_active(self, active: bool) -> None:
        """Set active gauge."""
        try:
            self._active.set(1 if active else 0)
        except Exception as e:
            logger.warning("metrics.set_emergency_active_failed", error=e)

    def record_activation(self, level: str, trigger_type: str) -> None:
        """Record an activation.

        level: normal|level_1|level_2|level_3
        trigger_type: manual|auto
        """
        try:
            is_synthetic = self._get_synthetic_label()
            # Normalize enum members to their value string recorder-side:
            # prometheus_client str()-coerces label values, and str() of a
            # (str, Enum) member is the member path ('EmergencyLevel.LEVEL_2'),
            # which would corrupt the exported label. Plain strings pass
            # through unchanged (str has no .value).
            self._activations_total.labels(
                level=getattr(level, "value", level),
                trigger_type=trigger_type,
                is_synthetic=is_synthetic,
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_emergency_activation_failed", error=e)

    def record_duration(self, level: str, duration: float) -> None:
        """Record emergency mode duration in seconds."""
        try:
            # Enum-to-value normalization; see record_activation.
            self._duration.labels(level=getattr(level, "value", level)).observe(
                duration
            )
        except Exception as e:
            logger.warning("metrics.record_emergency_duration_failed", error=e)

    def set_recovery_active(self, active: bool) -> None:
        """Set gradual recovery active gauge."""
        try:
            self._recovery_active.set(1 if active else 0)
        except Exception as e:
            logger.warning("metrics.set_recovery_active_failed", error=e)

    def record_recovery_step(self, from_level: str, to_level: str) -> None:
        """Record a recovery level step-down."""
        try:
            # Enum-to-value normalization; see record_activation.
            self._recovery_steps_total.labels(
                from_level=getattr(from_level, "value", from_level),
                to_level=getattr(to_level, "value", to_level),
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_recovery_step_failed", error=e)

    def record_recovery_rollback(self) -> None:
        """Record a recovery rollback (metrics check failure)."""
        try:
            self._recovery_rollbacks_total.inc()
        except Exception as e:
            logger.warning("metrics.record_recovery_rollback_failed", error=e)


# --- Module-level convenience functions (DD-7) ---


def _lazy_recorder() -> EmergencyModeMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "emergency_mode", None)
    except Exception:
        return None


def set_em_level(level: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_level(level)


def set_em_active(active: bool) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_active(active)


def record_em_activation(level: str, trigger_type: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_activation(level, trigger_type)


def record_em_duration(level: str, duration: float) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_duration(level, duration)


def set_em_recovery_active(active: bool) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_recovery_active(active)


def record_em_recovery_step(from_level: str, to_level: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_recovery_step(from_level, to_level)


def record_em_recovery_rollback() -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_recovery_rollback()
