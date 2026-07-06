"""
Canary Recovery metric recorder — rollout lifecycle metrics with
stage cardinality guard.

Uses sanitize_label_value() + max 20 distinct stage names,
overflow → "other". Based on EndpointNormalizer eviction pattern.

Metrics (4):
- baldur_canary_rollout_started_total: Rollout start counter
- baldur_canary_rollout_completed_total: Rollout completion counter
- baldur_canary_stage_advanced_total: Stage advancement counter
- baldur_canary_rollback_total: Rollback counter
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
)

logger = structlog.get_logger()

__all__ = [
    "CanaryMetricRecorder",
    "record_rollback",
    "record_rollout_completed",
    "record_rollout_started",
    "record_stage_advanced",
]

_MAX_STAGE_NAMES = 20


class CanaryMetricRecorder(BaseMetricRecorder):
    """Canary rollout lifecycle metric definitions and recording (4 metrics)."""

    def __init__(self) -> None:
        self._rollout_started = get_or_create_counter(
            f"{self.PREFIX}_canary_rollout_started_total",
            "Number of canary rollouts started",
            ["is_synthetic"],
        )
        self._rollout_completed = get_or_create_counter(
            f"{self.PREFIX}_canary_rollout_completed_total",
            "Number of canary rollouts completed",
            ["is_synthetic"],
        )
        self._stage_advanced = get_or_create_counter(
            f"{self.PREFIX}_canary_stage_advanced_total",
            "Number of stage advancements",
            ["stage_name", "is_synthetic"],
        )
        self._rollback_total = get_or_create_counter(
            f"{self.PREFIX}_canary_rollback_total",
            "Number of canary rollbacks",
            ["stage_name", "is_synthetic"],
        )

        self._seen_stages: OrderedDict[str, None] = OrderedDict()
        self._stage_lock = threading.Lock()

    def _guard_stage_name(self, stage_name: str) -> str:
        """Apply cardinality guard for stage_name label.

        Max 20 distinct stage names, overflow → "other".
        Thread-safe LRU eviction following EndpointNormalizer pattern.
        """
        from baldur.metrics.registry import sanitize_label_value

        sanitized = sanitize_label_value(stage_name)

        with self._stage_lock:
            if sanitized in self._seen_stages:
                self._seen_stages.move_to_end(sanitized)
                return sanitized

            if len(self._seen_stages) >= _MAX_STAGE_NAMES:
                return "other"

            self._seen_stages[sanitized] = None
            return sanitized

    def record_rollout_started(self) -> None:
        """Record a canary rollout start."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._rollout_started.labels(is_synthetic=is_synthetic).inc()
        except Exception:
            logger.debug("canary.metric_record_failed", metric="rollout_started")

    def record_rollout_completed(self) -> None:
        """Record a canary rollout completion."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._rollout_completed.labels(is_synthetic=is_synthetic).inc()
        except Exception:
            logger.debug("canary.metric_record_failed", metric="rollout_completed")

    def record_stage_advanced(self, stage_name: str) -> None:
        """Record a stage advancement."""
        try:
            is_synthetic = self._get_synthetic_label()
            guarded = self._guard_stage_name(stage_name)
            self._stage_advanced.labels(
                stage_name=guarded, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("canary.metric_record_failed", metric="stage_advanced")

    def record_rollback(self, stage_name: str) -> None:
        """Record a canary rollback."""
        try:
            is_synthetic = self._get_synthetic_label()
            guarded = self._guard_stage_name(stage_name)
            self._rollback_total.labels(
                stage_name=guarded, is_synthetic=is_synthetic
            ).inc()
        except Exception:
            logger.debug("canary.metric_record_failed", metric="rollback")


# =============================================================================
# Module-level recording shortcuts for the canary lifecycle recorder.
#
# Mirrors the circuit_breaker recorder's module-level shortcuts (lazy
# getattr-guarded recorder lookup) MINUS the sticky-fail cache: canary
# lifecycle calls are rare operator actions, not a hot path, so a plain
# per-call getattr is sufficient and needs no reset hook for test isolation.
# The getattr guard makes the calls backend-safe as defense-in-depth — the
# OTel backend already instantiates ``self.canary``, so the guard protects
# against partial-init / NoOp-backend states, not a missing recorder.
# =============================================================================


def _canary_recorder() -> CanaryMetricRecorder | None:
    """Look up the live canary recorder, returning None when unavailable."""
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "canary", None)
    except Exception:
        logger.debug("canary.recorder_unavailable")
        return None


def record_rollout_started() -> None:
    """Module-level shortcut for ``CanaryMetricRecorder.record_rollout_started``."""
    rec = _canary_recorder()
    if rec is not None:
        rec.record_rollout_started()


def record_rollout_completed() -> None:
    """Module-level shortcut for ``CanaryMetricRecorder.record_rollout_completed``."""
    rec = _canary_recorder()
    if rec is not None:
        rec.record_rollout_completed()


def record_stage_advanced(stage_name: str) -> None:
    """Module-level shortcut for ``CanaryMetricRecorder.record_stage_advanced``."""
    rec = _canary_recorder()
    if rec is not None:
        rec.record_stage_advanced(stage_name)


def record_rollback(stage_name: str) -> None:
    """Module-level shortcut for ``CanaryMetricRecorder.record_rollback``."""
    rec = _canary_recorder()
    if rec is not None:
        rec.record_rollback(stage_name)
