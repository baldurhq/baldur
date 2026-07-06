"""
Circuit Breaker metric recorder — metric definitions and recording.

Owns all Circuit Breaker-related Prometheus metrics.
Label name: ``service`` (D15 — unified from ``service_name``).
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
    "CBMetricRecorder",
    "record_blocked",
    "record_close_check_degraded_mode",
    "record_half_open_degraded_mode",
    "record_half_open_stuck_recovery",
    "record_open_check_degraded_mode",
    "record_peer_propagation",
    "reset_blocked_recorder",
]


class CBMetricRecorder(BaseMetricRecorder):
    """Circuit Breaker metric definitions and recording.

    D15: label name unified to ``service`` (was ``service_name`` in old system).
    D14: ``is_synthetic`` label added to transitions counter.
    476 D8/D10 added: blocked_total (with reason), half_open_degraded_mode_total,
    half_open_stuck_recovery_total.
    """

    def __init__(self) -> None:
        self._state = get_or_create_gauge(
            f"{self.PREFIX}_circuit_breaker_state",
            "Circuit breaker state (0=closed, 1=open, 2=half_open)",
            ["service", "cell_id"],
        )
        self._failures_total = get_or_create_counter(
            f"{self.PREFIX}_circuit_breaker_failures_total",
            "Total circuit breaker failures",
            ["service"],
        )
        self._trips_total = get_or_create_counter(
            f"{self.PREFIX}_circuit_breaker_trips_total",
            "Total times circuit breaker tripped to open",
            ["service"],
        )
        self._transitions_total = get_or_create_counter(
            f"{self.PREFIX}_circuit_breaker_transitions_total",
            "Total circuit breaker state transitions",
            ["service", "cell_id", "from_state", "to_state", "is_synthetic"],
        )
        self._open_duration = get_or_create_histogram(
            f"{self.PREFIX}_circuit_breaker_open_duration_seconds",
            "Duration in open state before closing",
            ["service"],
            buckets=(60, 300, 600, 1800, 3600, 7200),
        )
        # 476 D10: requests blocked by the CB. ``reason`` distinguishes
        # state-OPEN rejection from HALF_OPEN-window-full rejection so
        # operators can tell whether CB recovery is making progress.
        self._blocked_total = get_or_create_counter(
            f"{self.PREFIX}_circuit_breaker_blocked_total",
            "Total requests blocked by the circuit breaker",
            ["service", "reason"],
        )
        # 476 C1: HALF_OPEN slot acquisition fell through to L1 because L2
        # (Redis) was unhealthy or timed out. While this counter is non-zero,
        # the §392 "exactly N total" contract is relaxed to "≤ N per worker".
        self._half_open_degraded_mode_total = get_or_create_counter(
            f"{self.PREFIX}_circuit_breaker_half_open_degraded_mode_total",
            "Total HALF_OPEN slot acquisitions served by L1 fallback",
            ["service"],
        )
        # 476 D8: a HALF_OPEN window with count==limit was older than
        # ``half_open_stuck_timeout_seconds``, so the next acquire auto-reset
        # the window. Indicates worker churn during HALF_OPEN trials.
        self._half_open_stuck_recovery_total = get_or_create_counter(
            f"{self.PREFIX}_circuit_breaker_half_open_stuck_recovery_total",
            "Total HALF_OPEN windows auto-reset due to stalled trial detection",
            ["service"],
        )
        # 498 D7: HALF_OPEN -> CLOSED close-check fell back to L1 because L2
        # was unhealthy, timed out, raised, or returned a stale state. While
        # this counter is non-zero, the cross-process exactly-one contract
        # is relaxed to <=1 per worker.
        self._close_check_degraded_mode_total = get_or_create_counter(
            f"{self.PREFIX}_circuit_breaker_close_check_degraded_mode_total",
            (
                "Total HALF_OPEN->CLOSED close-checks served by L1 fallback "
                "(cross-process exactly-one contract relaxed to <=1 per worker "
                "-- covers L2 unhealthy, timeout, exception, and stale-L2 "
                "routing detection)."
            ),
            ["service"],
        )
        # 656 D7: HALF_OPEN -> OPEN re-open check fell back to L1 because L2
        # was unhealthy, timed out, raised, or returned a stale state. While
        # this counter is non-zero, the cross-process exactly-one contract is
        # relaxed to <=1 per worker (symmetric mirror of the close-check).
        self._open_check_degraded_mode_total = get_or_create_counter(
            f"{self.PREFIX}_circuit_breaker_open_check_degraded_mode_total",
            (
                "Total HALF_OPEN->OPEN re-open checks served by L1 fallback "
                "(cross-process exactly-one contract relaxed to <=1 per worker "
                "-- covers L2 unhealthy, timeout, exception, and stale-L2 "
                "routing detection)."
            ),
            ["service"],
        )
        # 656 D5: a peer worker's CB OPEN/CLOSED transition was applied to this
        # worker's L1 by the propagation listener. ``outcome`` is ``applied``
        # (L1 transitioned) or ``noop`` (idempotent re-apply). The repo-level
        # L1 apply bypasses the service's on_state_changed metric path, so the
        # listener also refreshes the cb_state gauge on an ``applied`` outcome
        # (R6) — without it the gauge would lie (report closed while rejecting).
        self._peer_propagation_total = get_or_create_counter(
            f"{self.PREFIX}_circuit_breaker_peer_propagation_total",
            "Total peer CB state transitions applied to L1 via propagation",
            ["service", "to_state", "outcome"],
        )

    def set_state(self, service: str, state: str, cell_id: str = "") -> None:
        """Set the circuit breaker state metric."""
        try:
            state_map = {"closed": 0, "open": 1, "half_open": 2}
            value = state_map.get(state, 0)
            self._state.labels(service=service, cell_id=cell_id).set(value)
        except Exception as e:
            logger.warning("metrics.set_circuit_breaker_failed", error=e)

    def record_failure(self, service: str) -> None:
        """Record a circuit breaker failure."""
        try:
            self._failures_total.labels(service=service).inc()
        except Exception as e:
            logger.warning("metrics.record_circuit_failure_failed", error=e)

    def record_trip(self, service: str) -> None:
        """Record a circuit breaker trip to open state."""
        try:
            self._trips_total.labels(service=service).inc()
        except Exception as e:
            logger.warning("metrics.record_circuit_trip_failed", error=e)

    def record_state_change(
        self,
        service: str,
        from_state: str,
        to_state: str,
        cell_id: str = "",
    ) -> None:
        """Record a circuit breaker state transition."""
        try:
            self.set_state(service, to_state, cell_id=cell_id)
            is_synthetic = self._get_synthetic_label()
            self._transitions_total.labels(
                service=service,
                cell_id=cell_id,
                from_state=from_state,
                to_state=to_state,
                is_synthetic=is_synthetic,
            ).inc()
            logger.info(
                "metrics.circuit_breaker_transition",
                target_service=service,
                from_state=from_state,
                to_state=to_state,
                is_synthetic=is_synthetic,
            )
        except Exception as e:
            logger.warning("metrics.record_circuit_breaker_failed", error=e)

    def record_open_duration(self, service: str, duration_seconds: float) -> None:
        """Record how long a circuit breaker was in open state."""
        try:
            self._open_duration.labels(service=service).observe(duration_seconds)
            logger.debug(
                "metrics.cb_open_duration_recorded",
                target_service=service,
                duration_seconds=duration_seconds,
            )
        except Exception as e:
            logger.warning("metrics.record_cb_duration_failed", error=e)

    def record_blocked(self, service: str, reason: str) -> None:
        """Record a request blocked by the CB (476 D10).

        ``reason`` values:
        - ``open``: state == OPEN
        - ``half_open_full``: HALF_OPEN window already at limit
        """
        try:
            self._blocked_total.labels(service=service, reason=reason).inc()
        except Exception as e:
            logger.warning("metrics.record_cb_blocked_failed", error=e)

    def record_half_open_degraded_mode(self, service: str) -> None:
        """Record an L1-fallback HALF_OPEN slot acquisition (476 C1)."""
        try:
            self._half_open_degraded_mode_total.labels(service=service).inc()
        except Exception as e:
            logger.warning("metrics.record_cb_half_open_degraded_failed", error=e)

    def record_half_open_stuck_recovery(self, service: str) -> None:
        """Record an auto-reset of a stalled HALF_OPEN window (476 D8)."""
        try:
            self._half_open_stuck_recovery_total.labels(service=service).inc()
        except Exception as e:
            logger.warning("metrics.record_cb_half_open_stuck_recovery_failed", error=e)

    def record_close_check_degraded_mode(self, service: str) -> None:
        """Record an L1-fallback HALF_OPEN->CLOSED close-check (498 D7)."""
        try:
            self._close_check_degraded_mode_total.labels(service=service).inc()
        except Exception as e:
            logger.warning("metrics.record_cb_close_check_degraded_failed", error=e)

    def record_open_check_degraded_mode(self, service: str) -> None:
        """Record an L1-fallback HALF_OPEN->OPEN re-open check (656 D7)."""
        try:
            self._open_check_degraded_mode_total.labels(service=service).inc()
        except Exception as e:
            logger.warning("metrics.record_cb_open_check_degraded_failed", error=e)

    def record_peer_propagation(
        self, service: str, to_state: str, outcome: str
    ) -> None:
        """Record a peer CB transition applied to L1 + refresh the gauge (656 D5).

        On an ``applied`` outcome, also refresh the ``circuit_breaker_state``
        gauge to ``to_state`` (R6): the repo-level peer apply bypasses the
        service ``on_state_changed`` metric path, so without this the gauge
        would report a stale state while the peer rejects traffic. A ``noop``
        re-apply records the counter only (no gauge change).
        """
        try:
            self._peer_propagation_total.labels(
                service=service, to_state=to_state, outcome=outcome
            ).inc()
            if outcome == "applied":
                # Composite key split at the metric boundary, mirroring
                # on_state_changed: the cb_state gauge is labeled
                # (base_service, cell_id), so refreshing with the raw composite
                # name would write a phantom cell_id="" series and leave the
                # canonical (base, cell_id) series stale — defeating the
                # gauge-must-not-lie guarantee for cell-based CB names. A
                # non-composite name resolves to (name, "") unchanged.
                from baldur.core.cb_namespace import parse_composite_cb_name

                base_service, cell_id = parse_composite_cb_name(service)
                self.set_state(base_service, to_state, cell_id=cell_id)
        except Exception as e:
            logger.warning("metrics.record_cb_peer_propagation_failed", error=e)


# =============================================================================
# Module-level sticky-flag cache for the CB recorder lookup.
#
# Mirrors ``metrics/recorders/protect.py`` (#480 DEC-2). The first failed
# lookup (``prometheus_client`` missing, BaldurMetrics not initialized, etc.)
# sets ``_cb_recorder_init_failed`` so subsequent rejects in the CB hot path
# return immediately instead of re-running the failing lookup. Recovery
# requires explicit ``reset_blocked_recorder()`` (wired into
# ``baldur.protect_facade.reset_protect_caches`` via the D7 reset chain).
# =============================================================================

_cb_recorder: CBMetricRecorder | None = None
_cb_recorder_init_failed: bool = False


def _lazy_recorder() -> CBMetricRecorder | None:
    global _cb_recorder, _cb_recorder_init_failed
    if _cb_recorder is not None:
        return _cb_recorder
    if _cb_recorder_init_failed:
        return None
    try:
        from baldur.metrics.prometheus import get_metrics

        recorder = getattr(get_metrics(), "circuit_breaker", None)
    except Exception as e:
        _cb_recorder_init_failed = True
        logger.warning("metrics.cb_recorder_unavailable_sticky", error=e)
        return None
    if recorder is None:
        _cb_recorder_init_failed = True
        return None
    _cb_recorder = recorder
    return _cb_recorder


def reset_blocked_recorder() -> None:
    """Reset the cached CB recorder and the sticky failure flag.

    Test isolation hook — wired into ``baldur.protect_facade.reset_protect_caches``
    so settings/recorder resets cascade to this module's sticky state.
    """
    global _cb_recorder, _cb_recorder_init_failed
    _cb_recorder = None
    _cb_recorder_init_failed = False


def record_blocked(service: str, reason: str) -> None:
    """Module-level shortcut for ``CBMetricRecorder.record_blocked`` (476 D10)."""
    rec = _lazy_recorder()
    if rec:
        rec.record_blocked(service, reason)


def record_half_open_degraded_mode(service: str) -> None:
    """Module-level shortcut for HALF_OPEN degraded-mode counter (476 C1)."""
    rec = _lazy_recorder()
    if rec:
        rec.record_half_open_degraded_mode(service)


def record_half_open_stuck_recovery(service: str) -> None:
    """Module-level shortcut for HALF_OPEN stuck-recovery counter (476 D8)."""
    rec = _lazy_recorder()
    if rec:
        rec.record_half_open_stuck_recovery(service)


def record_close_check_degraded_mode(service: str) -> None:
    """Module-level shortcut for close-check degraded-mode counter (498 D7)."""
    rec = _lazy_recorder()
    if rec:
        rec.record_close_check_degraded_mode(service)


def record_open_check_degraded_mode(service: str) -> None:
    """Module-level shortcut for open-check degraded-mode counter (656 D7)."""
    rec = _lazy_recorder()
    if rec:
        rec.record_open_check_degraded_mode(service)


def record_peer_propagation(service: str, to_state: str, outcome: str) -> None:
    """Module-level shortcut for the peer-propagation counter + gauge (656 D5)."""
    rec = _lazy_recorder()
    if rec:
        rec.record_peer_propagation(service, to_state, outcome)
