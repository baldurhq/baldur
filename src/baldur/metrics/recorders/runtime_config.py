"""
Runtime Config metric recorder — tracks configuration updates,
no-change events, safe default applications, and failures.

Metrics (5):
- baldur_runtime_config_updates_total: Config update counter
- baldur_runtime_config_update_no_change_total: No-change update counter
- baldur_runtime_config_safe_default_applied_total: Safe default application counter
- baldur_runtime_config_update_failed_total: Update failure counter
- baldur_runtime_config_pending_changes: Pending changes gauge
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
)

logger = structlog.get_logger()

__all__ = ["RuntimeConfigMetricRecorder"]


class RuntimeConfigMetricRecorder(BaseMetricRecorder):
    """Runtime Config metric definitions and recording (5 metrics)."""

    def __init__(self) -> None:
        self._updates_total = get_or_create_counter(
            f"{self.PREFIX}_runtime_config_updates_total",
            "Total runtime config updates",
            ["config_type", "is_synthetic"],
        )
        self._no_change_total = get_or_create_counter(
            f"{self.PREFIX}_runtime_config_update_no_change_total",
            "Config updates with no actual change",
            ["config_type"],
        )
        self._safe_default_applied_total = get_or_create_counter(
            f"{self.PREFIX}_runtime_config_safe_default_applied_total",
            "Safe default values applied during config update",
            ["config_type", "field"],
        )
        self._update_failed_total = get_or_create_counter(
            f"{self.PREFIX}_runtime_config_update_failed_total",
            "Config update failures",
            ["config_type", "reason"],
        )
        self._pending_changes = get_or_create_gauge(
            f"{self.PREFIX}_runtime_config_pending_changes",
            "Number of pending config changes",
            ["config_type"],
        )

    def record_update(self, config_type: str) -> None:
        """Record a successful config update."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._updates_total.labels(
                config_type=config_type, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("runtime_config.metric_record_failed", metric="update")

    def record_no_change(self, config_type: str) -> None:
        """Record a config update that resulted in no change."""
        try:
            self._no_change_total.labels(config_type=config_type).inc()
        except Exception:
            logger.debug("runtime_config.metric_record_failed", metric="no_change")

    def record_safe_default_applied(self, config_type: str, field: str) -> None:
        """Record a safe default value application."""
        try:
            self._safe_default_applied_total.labels(
                config_type=config_type, field=field
            ).inc()
        except Exception:
            logger.debug("runtime_config.metric_record_failed", metric="safe_default")

    def record_update_failed(self, config_type: str, reason: str) -> None:
        """Record a config update failure."""
        try:
            self._update_failed_total.labels(
                config_type=config_type, reason=reason
            ).inc()
        except Exception:
            logger.debug("runtime_config.metric_record_failed", metric="update_failed")

    def set_pending_changes(self, config_type: str, count: int) -> None:
        """Set the number of pending config changes."""
        try:
            self._pending_changes.labels(config_type=config_type).set(
                self._clamp_non_negative(count, "pending_changes")
            )
        except Exception:
            logger.debug(
                "runtime_config.metric_record_failed", metric="pending_changes"
            )
