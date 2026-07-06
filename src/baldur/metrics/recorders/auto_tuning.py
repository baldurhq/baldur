"""
Auto Tuning metric recorder — operational visibility for the
auto tuning service.

Provides quantitative data for monitoring tuning health, module states,
governance blocks, and safety bounds violations. This data drives
evidence-based refactoring decisions (see 358 trigger conditions).

Metrics (7):
- baldur_auto_tuning_enabled: Enabled state gauge
- baldur_auto_tuning_module_state: Per-module state gauge
- baldur_auto_tuning_adjustments_total: Adjustment count per module
- baldur_auto_tuning_override_active: Active override count
- baldur_auto_tuning_override_rollback_total: Override rollback count
- baldur_auto_tuning_governance_block_total: Governance block events
- baldur_auto_tuning_safety_bounds_violations_total: Safety bounds violations
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
)

logger = structlog.get_logger()

__all__ = ["AutoTuningMetricRecorder"]


class AutoTuningMetricRecorder(BaseMetricRecorder):
    """Auto Tuning metric definitions and recording (7 metrics)."""

    def __init__(self) -> None:
        self._enabled = get_or_create_gauge(
            f"{self.PREFIX}_auto_tuning_enabled",
            "Auto tuning enabled state (0/1)",
            [],
        )
        self._module_state = get_or_create_gauge(
            f"{self.PREFIX}_auto_tuning_module_state",
            "Per-module state (0=disabled, 1=enabled, 2=auto_disabled)",
            ["module"],
        )
        self._adjustments_total = get_or_create_counter(
            f"{self.PREFIX}_auto_tuning_adjustments_total",
            "Total adjustments per module",
            ["module"],
        )
        self._override_active = get_or_create_gauge(
            f"{self.PREFIX}_auto_tuning_override_active",
            "Number of active overrides",
            ["parameter"],
        )
        self._override_rollback_total = get_or_create_counter(
            f"{self.PREFIX}_auto_tuning_override_rollback_total",
            "Override rollback count",
            [],
        )
        self._governance_block_total = get_or_create_counter(
            f"{self.PREFIX}_auto_tuning_governance_block_total",
            "Governance block events by check type",
            ["check_type"],
        )
        self._safety_bounds_violations_total = get_or_create_counter(
            f"{self.PREFIX}_auto_tuning_safety_bounds_violations_total",
            "Safety bounds violation count",
            [],
        )

    def set_enabled(self, enabled: bool) -> None:
        """Set auto tuning enabled state."""
        try:
            self._enabled.labels().set(1 if enabled else 0)
        except Exception:
            logger.debug("auto_tuning.metric_record_failed", metric="enabled")

    def set_module_state(self, module: str, state: int) -> None:
        """Set module state (0=disabled, 1=enabled, 2=auto_disabled)."""
        try:
            self._module_state.labels(module=module).set(state)
        except Exception:
            logger.debug("auto_tuning.metric_record_failed", metric="module_state")

    def record_adjustment(self, module: str) -> None:
        """Record a tuning adjustment for a module."""
        try:
            self._adjustments_total.labels(module=module).inc()
        except Exception:
            logger.debug("auto_tuning.metric_record_failed", metric="adjustments")

    def set_override_active(self, parameter: str, count: int) -> None:
        """Set the number of active overrides for a parameter."""
        try:
            self._override_active.labels(parameter=parameter).set(
                self._clamp_non_negative(count, "override_active")
            )
        except Exception:
            logger.debug("auto_tuning.metric_record_failed", metric="override_active")

    def record_override_rollback(self) -> None:
        """Record an override rollback."""
        try:
            self._override_rollback_total.labels().inc()
        except Exception:
            logger.debug("auto_tuning.metric_record_failed", metric="override_rollback")

    def record_governance_block(self, check_type: str) -> None:
        """Record a governance block event."""
        try:
            self._governance_block_total.labels(check_type=check_type).inc()
        except Exception:
            logger.debug("auto_tuning.metric_record_failed", metric="governance_block")

    def record_safety_bounds_violation(self) -> None:
        """Record a safety bounds violation."""
        try:
            self._safety_bounds_violations_total.labels().inc()
        except Exception:
            logger.debug(
                "auto_tuning.metric_record_failed",
                metric="safety_bounds_violation",
            )
