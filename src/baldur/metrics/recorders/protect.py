"""
Protect facade metric recorder.

Owns the three Prometheus metrics emitted by ``baldur.protect()``:
- ``baldur_protect_attempts`` — histogram of attempts per call
- ``baldur_protect_duration_seconds`` — histogram of end-to-end duration
- ``baldur_protect_fallback_total`` — counter of fallback activations

Reference:
    429 — Part 1, C4
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_histogram,
)

logger = structlog.get_logger()

__all__ = ["ProtectMetricRecorder"]


class ProtectMetricRecorder(BaseMetricRecorder):
    """Metric definitions and recording for the ``baldur.protect()`` facade."""

    def __init__(self) -> None:
        self._attempts = get_or_create_histogram(
            f"{self.PREFIX}_protect_attempts",
            "Number of policy attempts per protect() call",
            ["name", "outcome", "mode"],
            buckets=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        )
        self._duration_seconds = get_or_create_histogram(
            f"{self.PREFIX}_protect_duration_seconds",
            "End-to-end duration of a protect() call in seconds",
            ["name", "outcome", "mode"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        self._fallback_total = get_or_create_counter(
            f"{self.PREFIX}_protect_fallback_total",
            "Total number of fallback activations inside protect()",
            ["name", "mode"],
        )

    def record(
        self,
        name: str,
        outcome: str,
        attempts: int,
        duration_seconds: float,
        fallback_used: bool,
        mode: str = "sync",
    ) -> None:
        """Record a single protect() invocation.

        Args:
            name: Service identifier passed to ``protect(name=...)``.
            outcome: One of ``"success"``, ``"fallback"``, ``"failure"``, ``"rejected"``.
            attempts: Total policy attempts (1 when no retry occurred).
            duration_seconds: Wall-clock duration in seconds.
            fallback_used: Whether the fallback branch produced the returned value.
            mode: Facade that emitted this call — ``"sync"`` (``protect``) or
                ``"async"`` (``aprotect``). Lets a ``name`` protected via BOTH
                facades be separated (e.g. a mode-specific latency SLO). Cardinality
                is self-limiting: Prometheus materializes only observed series, so a
                single-mode ``name`` keeps exactly one series.
        """
        try:
            self._attempts.labels(name=name, outcome=outcome, mode=mode).observe(
                attempts
            )
            self._duration_seconds.labels(
                name=name, outcome=outcome, mode=mode
            ).observe(duration_seconds)
            if fallback_used:
                self._fallback_total.labels(name=name, mode=mode).inc()
        except Exception as e:
            logger.warning("metrics.record_protect_failed", error=e)


# =============================================================================
# Module-level singleton — used by protect.py facade.
#
# baldur.protect() records via this singleton rather than via
# get_metrics().protect. Both metrics backends also construct their own
# ProtectMetricRecorder as the `protect` family attribute (the OTel backend for
# G46 family parity), so two ProtectMetricRecorder instances are live at once.
# That is double-count-safe by construction: every instance backs the *same*
# prometheus series via get_or_create_* (idempotent registration returns the
# already-registered collector), so the dual access path records once per call,
# not twice.
# =============================================================================

_recorder: ProtectMetricRecorder | None = None
_recorder_init_failed: bool = False


def get_protect_recorder() -> ProtectMetricRecorder | None:
    """Return the lazy ProtectMetricRecorder singleton, or None if prometheus_client missing.

    On first construction failure (e.g., ``prometheus_client`` not installed),
    sets the sticky ``_recorder_init_failed`` flag so subsequent calls return
    None immediately without re-running the failing constructor. Recovery
    requires explicit ``reset_protect_recorder()``.
    """
    global _recorder, _recorder_init_failed
    if _recorder is not None:
        return _recorder
    if _recorder_init_failed:
        return None
    try:
        _recorder = ProtectMetricRecorder()
    except Exception as e:
        _recorder_init_failed = True
        logger.warning("metrics.protect_recorder_unavailable_sticky", error=e)
        _recorder = None
    return _recorder


def reset_protect_recorder() -> None:
    """Reset the singleton and the sticky failure flag — for test isolation."""
    global _recorder, _recorder_init_failed
    _recorder = None
    _recorder_init_failed = False
