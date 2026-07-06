"""
Throttle metric recorder — metric definitions and recording for Adaptive Throttle.

Owns all throttle-related Prometheus metrics. Metrics are created via
get_or_create_* to avoid duplicate registration errors.

Replaces the 8 module-level _record_* functions and 24+ metric variables
previously in services/throttle/adaptive/__init__.py.
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

__all__ = ["ThrottleMetricRecorder"]


class ThrottleMetricRecorder(BaseMetricRecorder):
    """Throttle metric definitions and recording (8 sub-methods + top-level dispatcher)."""

    def __init__(self) -> None:
        # -- Core metrics --
        self._current_limit = get_or_create_gauge(
            f"{self.PREFIX}_throttle_limit",
            "Current throttle limit value",
            ["service"],
        )
        self._rtt_histogram = get_or_create_histogram(
            f"{self.PREFIX}_throttle_rtt_ms",
            "Response time (RTT) in milliseconds",
            ["service"],
            buckets=(10, 25, 50, 100, 200, 500, 1000, 2000, 5000),
        )
        self._gradient_gauge = get_or_create_gauge(
            f"{self.PREFIX}_throttle_gradient",
            "Current RTT gradient (positive=slowing, negative=improving)",
            ["service"],
        )

        # -- Request metrics --
        self._requests_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_requests_total",
            "Total requests processed by throttle",
            ["service", "result"],
        )
        self._allowed_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_allowed_total",
            "Total requests allowed by throttle",
            ["service"],
        )
        self._denied_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_denied_total",
            "Total requests denied by throttle",
            ["service", "reason"],
        )

        # -- SLA metrics --
        self._sla_warnings_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_sla_warnings_total",
            "Total SLA warning threshold breaches",
            ["service"],
        )
        self._sla_criticals_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_sla_criticals_total",
            "Total SLA critical threshold breaches",
            ["service"],
        )

        # -- Emergency / CB metrics --
        self._emergency_level_gauge = get_or_create_gauge(
            f"{self.PREFIX}_throttle_emergency_level",
            "Current emergency level (0-3)",
            ["service"],
        )
        self._emergency_adjustments_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_emergency_adjustments_total",
            "Total throttle limit adjustments due to emergency mode",
            ["level"],
        )
        self._gradient_frozen_gauge = get_or_create_gauge(
            f"{self.PREFIX}_throttle_gradient_frozen",
            "Whether gradient adjustment is frozen (1=yes, 0=no)",
            ["service"],
        )
        self._cb_adjustments_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_cb_adjustments_total",
            "Total throttle limit adjustments due to circuit breaker state",
            ["service", "cb_state"],
        )

        # -- Recovery / Full Stop metrics --
        self._recovery_active_gauge = get_or_create_gauge(
            f"{self.PREFIX}_throttle_recovery_dampening_active",
            "Whether recovery dampening is active (1=yes, 0=no)",
            ["service"],
        )
        self._recovery_step_gauge = get_or_create_gauge(
            f"{self.PREFIX}_throttle_recovery_dampening_step",
            "Current recovery dampening step (0=80%, 1=90%, 2=100%)",
            ["service"],
        )
        self._full_stop_gauge = get_or_create_gauge(
            f"{self.PREFIX}_throttle_full_stop_active",
            "Whether full stop is active (1=yes, 0=no)",
            ["service"],
        )
        self._full_stop_activations_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_full_stop_activations_total",
            "Total full stop activations",
            ["service", "reason"],
        )

        # -- Limit change metrics --
        self._limit_changes_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_limit_changes_total",
            "Total throttle limit changes",
            ["service", "direction", "trigger"],
        )
        self._limit_change_magnitude = get_or_create_histogram(
            f"{self.PREFIX}_throttle_limit_change_magnitude",
            "Magnitude of limit changes (percentage)",
            ["service", "direction"],
            buckets=(5, 10, 20, 30, 50, 70, 100),
        )

        # -- Saturation metrics --
        self._saturation_ratio = get_or_create_gauge(
            f"{self.PREFIX}_throttle_saturation_ratio",
            "Throttle limit saturation (current_limit / max_limit), 0.0-1.0. "
            "Lower values mean more throttling is applied",
            ["service"],
        )
        self._max_limit_gauge = get_or_create_gauge(
            f"{self.PREFIX}_throttle_max_limit",
            "Configured maximum throttle limit",
            ["service"],
        )

        # -- Error Budget integration metrics --
        self._error_budget_adjustments_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_error_budget_adjustments_total",
            "Total throttle limit adjustments triggered by error budget status changes",
            ["service", "budget_status"],
        )
        self._error_budget_multiplier_gauge = get_or_create_gauge(
            f"{self.PREFIX}_throttle_error_budget_multiplier",
            "Current error budget multiplier applied to throttle limit (0.0-1.0)",
            ["service"],
        )
        self._error_budget_reduction_active_gauge = get_or_create_gauge(
            f"{self.PREFIX}_throttle_error_budget_reduction_active",
            "Whether error budget limit reduction is active (1=yes, 0=no)",
            ["service"],
        )
        self._error_budget_preemptive_total = get_or_create_counter(
            f"{self.PREFIX}_throttle_error_budget_preemptive_total",
            "Total preemptive throttle reductions based on budget depletion forecast",
            ["service", "risk_level"],
        )

    # =========================================================================
    # Sub-recording methods (mirror the 8 _record_* functions)
    # =========================================================================

    def record_core_metrics(
        self,
        service: str,
        limit: int | None,
        rtt_ms: float | None,
        gradient: float | None,
        exemplar: dict | None,
    ) -> None:
        """Record core throttle metrics (limit, rtt, gradient)."""
        if limit is not None:
            self._current_limit.labels(service=service).set(limit)

        if rtt_ms is not None:
            try:
                self._rtt_histogram.labels(service=service).observe(
                    rtt_ms, exemplar=exemplar
                )
            except TypeError:
                self._rtt_histogram.labels(service=service).observe(rtt_ms)

        if gradient is not None:
            self._gradient_gauge.labels(service=service).set(gradient)

    def record_request_metrics(
        self,
        service: str,
        request_result: str | None,
        denied_reason: str | None,
        exemplar: dict | None,
    ) -> None:
        """Record request allowed/denied metrics."""
        if request_result is None:
            return

        self._requests_total.labels(service=service, result=request_result).inc()
        if request_result == "allowed":
            try:
                self._allowed_total.labels(service=service).inc(exemplar=exemplar)
            except TypeError:
                self._allowed_total.labels(service=service).inc()
        elif request_result == "denied" and denied_reason:
            self._denied_total.labels(service=service, reason=denied_reason).inc()

    def record_sla_metrics(self, service: str, sla_event: str | None) -> None:
        """Record SLA warning/critical metrics."""
        if sla_event == "warning":
            self._sla_warnings_total.labels(service=service).inc()
        elif sla_event == "critical":
            self._sla_criticals_total.labels(service=service).inc()

    def record_emergency_cb_metrics(
        self,
        service: str,
        emergency_level: int | None,
        gradient_frozen: bool | None,
        cb_state: str | None,
    ) -> None:
        """Record emergency / circuit breaker metrics."""
        if emergency_level is not None:
            self._emergency_level_gauge.labels(service=service).set(emergency_level)
            self._emergency_adjustments_total.labels(level=str(emergency_level)).inc()

        if gradient_frozen is not None:
            self._gradient_frozen_gauge.labels(service=service).set(
                1 if gradient_frozen else 0
            )

        if cb_state is not None:
            self._cb_adjustments_total.labels(service=service, cb_state=cb_state).inc()

    def record_recovery_full_stop_metrics(
        self,
        service: str,
        recovery_dampening_active: bool | None,
        recovery_dampening_step: int | None,
        full_stop_active: bool | None,
        full_stop_reason: str | None,
    ) -> None:
        """Record recovery and full stop metrics."""
        if recovery_dampening_active is not None:
            self._recovery_active_gauge.labels(service=service).set(
                1 if recovery_dampening_active else 0
            )

        if recovery_dampening_step is not None:
            self._recovery_step_gauge.labels(service=service).set(
                recovery_dampening_step
            )

        if full_stop_active is not None:
            self._full_stop_gauge.labels(service=service).set(
                1 if full_stop_active else 0
            )

        if full_stop_reason is not None:
            self._full_stop_activations_total.labels(
                service=service, reason=full_stop_reason
            ).inc()

    def record_limit_change_metrics(
        self,
        service: str,
        limit_change_direction: str | None,
        limit_change_trigger: str | None,
        limit_change_percent: float | None,
    ) -> None:
        """Record limit change metrics."""
        if not (limit_change_direction and limit_change_trigger):
            return

        self._limit_changes_total.labels(
            service=service,
            direction=limit_change_direction,
            trigger=limit_change_trigger,
        ).inc()

        if limit_change_percent is not None:
            self._limit_change_magnitude.labels(
                service=service,
                direction=limit_change_direction,
            ).observe(abs(limit_change_percent))

    def record_saturation_metrics(
        self, service: str, limit: int | None, max_limit: int | None
    ) -> None:
        """Record saturation ratio metrics."""
        if limit is not None and max_limit is not None and max_limit > 0:
            saturation = limit / max_limit
            self._saturation_ratio.labels(service=service).set(saturation)
            self._max_limit_gauge.labels(service=service).set(max_limit)

    def record_error_budget_metrics(
        self,
        service: str,
        error_budget_status: str | None,
        error_budget_multiplier: float | None,
        error_budget_reduction_active: bool | None,
        error_budget_preemptive_risk_level: str | None,
    ) -> None:
        """Record error budget integration metrics."""
        if error_budget_status is not None and self._error_budget_adjustments_total:
            self._error_budget_adjustments_total.labels(
                service=service,
                budget_status=error_budget_status,
            ).inc()

        if error_budget_multiplier is not None and self._error_budget_multiplier_gauge:
            self._error_budget_multiplier_gauge.labels(service=service).set(
                error_budget_multiplier
            )

        if (
            error_budget_reduction_active is not None
            and self._error_budget_reduction_active_gauge
        ):
            self._error_budget_reduction_active_gauge.labels(service=service).set(
                1 if error_budget_reduction_active else 0
            )

        if (
            error_budget_preemptive_risk_level is not None
            and self._error_budget_preemptive_total
        ):
            self._error_budget_preemptive_total.labels(
                service=service,
                risk_level=error_budget_preemptive_risk_level,
            ).inc()

    # =========================================================================
    # Top-level dispatcher (same signature as _record_throttle_metrics)
    # =========================================================================

    def record_throttle_metrics(
        self,
        service: str,
        # Core parameters (backward compatible)
        limit: int | None = None,
        rtt_ms: float | None = None,
        gradient: float | None = None,
        denied_reason: str | None = None,
        emergency_level: int | None = None,
        cb_state: str | None = None,
        # Extended parameters
        request_result: str | None = None,
        sla_event: str | None = None,
        gradient_frozen: bool | None = None,
        recovery_dampening_active: bool | None = None,
        recovery_dampening_step: int | None = None,
        full_stop_active: bool | None = None,
        full_stop_reason: str | None = None,
        limit_change_direction: str | None = None,
        limit_change_trigger: str | None = None,
        limit_change_percent: float | None = None,
        max_limit: int | None = None,
        trace_id: str | None = None,
        # Error Budget integration parameters
        error_budget_status: str | None = None,
        error_budget_multiplier: float | None = None,
        error_budget_reduction_active: bool | None = None,
        error_budget_preemptive_risk_level: str | None = None,
    ) -> None:
        """
        Record throttle Prometheus metrics (extended version).

        Backward compatible with original signature.
        Exemplar is Fail-Open: attachment failure does not stop recording.
        """
        try:
            exemplar = {"trace_id": trace_id} if trace_id else None

            self.record_core_metrics(service, limit, rtt_ms, gradient, exemplar)
            self.record_request_metrics(
                service, request_result, denied_reason, exemplar
            )
            self.record_sla_metrics(service, sla_event)
            self.record_emergency_cb_metrics(
                service, emergency_level, gradient_frozen, cb_state
            )
            self.record_recovery_full_stop_metrics(
                service,
                recovery_dampening_active,
                recovery_dampening_step,
                full_stop_active,
                full_stop_reason,
            )
            self.record_limit_change_metrics(
                service,
                limit_change_direction,
                limit_change_trigger,
                limit_change_percent,
            )
            self.record_saturation_metrics(service, limit, max_limit)
            self.record_error_budget_metrics(
                service,
                error_budget_status,
                error_budget_multiplier,
                error_budget_reduction_active,
                error_budget_preemptive_risk_level,
            )

        except Exception as e:
            logger.debug(
                "adaptive_throttle.metrics_failed",
                error=e,
            )
