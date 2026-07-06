"""
System Control metric recorder — metric definitions and recording.

Owns all System Control (kill switch) related Prometheus metrics.
State is 2D: enabled (bool) x dry_run (bool).
See DD-5 for SRE-Core scope rationale.
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
    "SystemControlMetricRecorder",
    "set_sc_enabled",
    "set_sc_dry_run",
    "record_sc_state_change",
    "record_sc_disabled_duration",
    "record_sc_disabled",
]


class SystemControlMetricRecorder(BaseMetricRecorder):
    """System Control metric definitions and recording (5 methods).

    DD-6: String interface for action labels.
    """

    def __init__(self) -> None:
        self._enabled = get_or_create_gauge(
            f"{self.PREFIX}_system_control_enabled",
            "1=enabled, 0=disabled (kill switch)",
            [],
        )
        self._dry_run = get_or_create_gauge(
            f"{self.PREFIX}_system_control_dry_run",
            "1=dry_run active, 0=live mode",
            [],
        )
        self._state_changes_total = get_or_create_counter(
            f"{self.PREFIX}_system_control_state_changes_total",
            "State change count by action",
            ["action"],
        )
        self._disabled_duration = get_or_create_histogram(
            f"{self.PREFIX}_system_control_disabled_duration_seconds",
            "Duration of each disabled period (recorded on re-enable)",
            [],
            buckets=(60, 300, 600, 1800, 3600, 7200, 14400, 86400),
        )
        self._disabled_total = get_or_create_counter(
            f"{self.PREFIX}_system_control_disabled_total",
            "Cumulative disable count",
            [],
        )

    def set_enabled(self, enabled: bool) -> None:
        """Set the enabled gauge."""
        try:
            self._enabled.set(1 if enabled else 0)
        except Exception as e:
            logger.warning("metrics.set_sc_enabled_failed", error=e)

    def set_dry_run(self, dry_run: bool) -> None:
        """Set the dry_run gauge."""
        try:
            self._dry_run.set(1 if dry_run else 0)
        except Exception as e:
            logger.warning("metrics.set_sc_dry_run_failed", error=e)

    def record_state_change(self, action: str) -> None:
        """Record a state change.

        action: enable|disable|enable_dry_run|disable_dry_run|reset
        """
        try:
            self._state_changes_total.labels(action=action).inc()
        except Exception as e:
            logger.warning("metrics.record_sc_state_change_failed", error=e)

    def record_disabled_duration(self, duration: float) -> None:
        """Record how long the system was disabled (called on re-enable)."""
        try:
            self._disabled_duration.observe(duration)
        except Exception as e:
            logger.warning("metrics.record_sc_disabled_duration_failed", error=e)

    def record_disabled(self) -> None:
        """Increment disabled count."""
        try:
            self._disabled_total.inc()
        except Exception as e:
            logger.warning("metrics.record_sc_disabled_failed", error=e)


# --- Module-level convenience functions (DD-7) ---


def _lazy_recorder() -> SystemControlMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "system_control", None)
    except Exception:
        return None


def set_sc_enabled(enabled: bool) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_enabled(enabled)


def set_sc_dry_run(dry_run: bool) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_dry_run(dry_run)


def record_sc_state_change(action: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_state_change(action)


def record_sc_disabled_duration(duration: float) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_disabled_duration(duration)


def record_sc_disabled() -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_disabled()
