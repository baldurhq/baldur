"""
Replay metric recorder — metric definitions and recording.

Owns all Replay-related Prometheus metrics.
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_histogram,
)

logger = structlog.get_logger()

__all__ = ["ReplayMetricRecorder"]


class ReplayMetricRecorder(BaseMetricRecorder):
    """Replay metric definitions and recording (2 methods)."""

    def __init__(self) -> None:
        self._attempts_total = get_or_create_counter(
            f"{self.PREFIX}_replay_attempts_total",
            "Total replay attempts",
            ["domain", "replay_type", "is_synthetic"],
        )
        self._outcomes_total = get_or_create_counter(
            f"{self.PREFIX}_replay_outcomes_total",
            "Replay outcomes",
            ["domain", "outcome", "is_synthetic"],
        )
        self._duration_seconds = get_or_create_histogram(
            f"{self.PREFIX}_replay_duration_seconds",
            "Replay operation duration",
            ["domain"],
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30),
        )

    def record_started(self, domain: str, replay_type: str) -> None:
        """Record a replay start (attempts counter only).

        The outcome is unknown at start, so only the attempts counter is
        incremented — unlike record_attempt, which also bumps outcomes.
        """
        try:
            is_synthetic = self._get_synthetic_label()
            self._attempts_total.labels(
                domain=domain,
                replay_type=replay_type,
                is_synthetic=is_synthetic,
            ).inc()
            logger.debug(
                "metrics.replay_started",
                healing_domain=domain,
                replay_type=replay_type,
                is_synthetic=is_synthetic,
            )
        except Exception as e:
            logger.warning("metrics.record_replay_metric_failed", error=e)

    def record_attempt(self, domain: str, replay_type: str, success: bool) -> None:
        """Record a replay attempt."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._attempts_total.labels(
                domain=domain,
                replay_type=replay_type,
                is_synthetic=is_synthetic,
            ).inc()
            outcome = "success" if success else "failure"
            self._outcomes_total.labels(
                domain=domain,
                outcome=outcome,
                is_synthetic=is_synthetic,
            ).inc()
            logger.debug(
                "metrics.replay_recorded",
                healing_domain=domain,
                replay_type=replay_type,
                success=success,
                is_synthetic=is_synthetic,
            )
        except Exception as e:
            logger.warning("metrics.record_replay_metric_failed", error=e)

    def record_replay(
        self, domain: str, result: str, duration: float | None = None
    ) -> None:
        """Record a replay operation."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._outcomes_total.labels(
                domain=domain,
                outcome=result,
                is_synthetic=is_synthetic,
            ).inc()
            if duration is not None:
                self._duration_seconds.labels(domain=domain).observe(duration)
        except Exception as e:
            logger.warning("metrics.record_replay_metric_failed", error=e)
