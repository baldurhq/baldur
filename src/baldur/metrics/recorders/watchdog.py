"""
Meta-Watchdog metric recorder — metric definitions and recording.

Owns all Watchdog-related Prometheus metrics.
Component label cardinality bounded by the _ALLOWED_COMPONENTS frozenset
(_COMPONENT_PRIORITY ∪ {event_bus, celery, database}). Prevents time-series
explosion if components are added dynamically at ENT tier.
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
    "WatchdogMetricRecorder",
    "record_watchdog_probe",
    "record_watchdog_recovery",
    "record_watchdog_escalation",
    "record_watchdog_governance_blocked",
    "set_watchdog_self_cb_state",
    "observe_watchdog_probe_duration",
]

# Bounded component set — _COMPONENT_PRIORITY (the PRO watchdog's per-component
# priority map) ∪ {event_bus, celery, database} (infrastructure components with
# no priority entry). Unknown components are mapped to "other" to prevent
# cardinality explosion. Every _COMPONENT_PRIORITY key MUST appear here, or its
# escalation/probe metrics are silently labeled "other" — enforced by the
# coupling test in test_watchdog_recorder.py.
_ALLOWED_COMPONENTS: frozenset[str] = frozenset(
    {
        # _COMPONENT_PRIORITY keys
        "redis",
        "dlq",
        "circuit_breaker",
        "recovery_pipeline",
        "audit_system",
        "chaos_scheduler",
        "notification_channels",
        "precomputed_cache",
        "error_budget_gate",
        "canary_rollout",
        "emergency_mode",
        "adaptive_throttle",
        # Infrastructure components (no _COMPONENT_PRIORITY entry)
        "event_bus",
        "celery",
        "database",
    }
)
_FALLBACK_COMPONENT = "other"


def _resolve_component(component: str) -> str:
    """Map component to allowed value or fallback."""
    return component if component in _ALLOWED_COMPONENTS else _FALLBACK_COMPONENT


class WatchdogMetricRecorder(BaseMetricRecorder):
    """Meta-Watchdog metric definitions and recording."""

    def __init__(self) -> None:
        self._probe_total = get_or_create_counter(
            f"{self.PREFIX}_watchdog_probe_total",
            "Probe success/failure per component",
            ["component", "status"],
        )
        self._recovery_total = get_or_create_counter(
            f"{self.PREFIX}_watchdog_recovery_total",
            "Recovery attempts and outcomes per component",
            ["component", "action", "result"],
        )
        self._self_cb_state = get_or_create_gauge(
            f"{self.PREFIX}_watchdog_self_cb_state",
            "Self circuit breaker state (0=closed, 1=open)",
            [],
        )
        self._recovery_governance_blocked_total = get_or_create_counter(
            f"{self.PREFIX}_watchdog_recovery_governance_blocked_total",
            "Recovery attempts blocked by governance",
            ["component"],
        )
        self._escalation_total = get_or_create_counter(
            f"{self.PREFIX}_watchdog_escalation_total",
            "Escalations per component and delivery result",
            ["component", "result"],
        )
        self._probe_duration = get_or_create_histogram(
            f"{self.PREFIX}_watchdog_probe_duration_seconds",
            "Per-component probe latency",
            ["component"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
        )

    def record_probe(self, component: str, status: str) -> None:
        """Record a watchdog probe result.

        component: redis|dlq|circuit_breaker|recovery_pipeline|chaos_scheduler|...
        status: success|failure
        """
        try:
            self._probe_total.labels(
                component=_resolve_component(component), status=status
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_watchdog_probe_failed", error=e)

    def record_recovery(self, component: str, action: str, result: str) -> None:
        """Record a watchdog recovery attempt.

        component: target component name
        action: recovery action taken (e.g. restart, reset, escalate)
        result: success|failure|skipped
        """
        try:
            self._recovery_total.labels(
                component=_resolve_component(component),
                action=action,
                result=result,
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_watchdog_recovery_failed", error=e)

    def set_self_cb_state(self, is_open: bool) -> None:
        """Set self circuit breaker state gauge."""
        try:
            self._self_cb_state.set(1 if is_open else 0)
        except Exception as e:
            logger.warning("metrics.set_watchdog_self_cb_state_failed", error=e)

    def record_governance_blocked(self, component: str) -> None:
        """Record a watchdog recovery attempt blocked by governance."""
        try:
            self._recovery_governance_blocked_total.labels(
                component=_resolve_component(component)
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_watchdog_governance_blocked_failed", error=e)

    def record_escalation(self, component: str, result: str) -> None:
        """Record a watchdog escalation.

        component: target component name
        result: sent|fallback|suppressed
        """
        try:
            self._escalation_total.labels(
                component=_resolve_component(component), result=result
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_watchdog_escalation_failed", error=e)

    def observe_probe_duration(self, component: str, duration: float) -> None:
        """Record probe latency for a component."""
        try:
            self._probe_duration.labels(
                component=_resolve_component(component)
            ).observe(duration)
        except Exception as e:
            logger.warning("metrics.observe_watchdog_probe_duration_failed", error=e)


# --- Module-level convenience functions (DD-7) ---


def _lazy_recorder() -> WatchdogMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "watchdog", None)
    except Exception:
        return None


def record_watchdog_probe(component: str, status: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_probe(component, status)


def record_watchdog_recovery(component: str, action: str, result: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_recovery(component, action, result)


def set_watchdog_self_cb_state(is_open: bool) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.set_self_cb_state(is_open)


def record_watchdog_governance_blocked(component: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_governance_blocked(component)


def record_watchdog_escalation(component: str, result: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_escalation(component, result)


def observe_watchdog_probe_duration(component: str, duration: float) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.observe_probe_duration(component, duration)
