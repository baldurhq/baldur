"""
Metric Recording Functions — External safe entry points.

All record_* functions apply domain cardinality guard (resolve_domain_label)
and try-except wrapping before delegating to domain-specific recorders
via get_metrics().

For domain metrics (DLQ, Retry, CB, Replay), delegates to:
    get_metrics().dlq / .retry / .circuit_breaker / .replay

Non-domain metrics (L2, Error Budget, Heartbeat, Fail-Safe, X-Test)
use get_or_create_* directly.

For synthetic requests (X-Test-Mode, Chaos experiments), the is_synthetic label is set automatically.
"""

from __future__ import annotations

import time
from datetime import datetime

import structlog

from baldur.core.test_mode_context import TestModeContext
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
    get_or_create_histogram,
    resolve_domain_label,
)

logger = structlog.get_logger()

# =============================================================================
# Non-domain metric definitions (formerly in definitions.py)
# These metrics are NOT part of the core domain recorders.
# =============================================================================

# L2 Storage
_l2_timeout_total = get_or_create_counter(
    "baldur_l2_timeout_total",
    "Total L2 storage timeout occurrences",
    ["adapter_type", "operation"],
)
_l2_sync_failure_total = get_or_create_counter(
    "baldur_l2_sync_failure_total",
    "Total L2 storage sync failures",
    ["adapter_type", "operation"],
)
_l2_latency_seconds = get_or_create_histogram(
    "baldur_l2_latency_seconds",
    "L2 storage operation latency in seconds",
    ["adapter_type"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
_l2_connection_status = get_or_create_gauge(
    "baldur_l2_connection_status",
    "L2 storage connection status (1=healthy, 0=unhealthy)",
    ["adapter_type"],
)

# Recovery (non-domain — used alongside DLQ/Retry domain recorders)
_recovery_time_seconds = get_or_create_histogram(
    "baldur_recovery_time_seconds",
    "Time from failure to resolution in seconds",
    ["domain", "resolution_type"],
    buckets=(60, 300, 900, 1800, 3600, 7200, 14400, 28800, 86400),
)
_sla_breach_total = get_or_create_counter(
    "baldur_sla_breach_total",
    "Total SLA breaches detected",
    ["domain"],
)

# Error Budget
_error_budget_remaining_percent = get_or_create_gauge(
    "baldur_error_budget_remaining_percent",
    "Error budget remaining as percentage (0-100)",
    ["slo_name", "is_synthetic", "region", "tier"],
)
_error_budget_remaining_minutes = get_or_create_gauge(
    "baldur_error_budget_remaining_minutes",
    "Error budget remaining in minutes",
    ["slo_name", "is_synthetic", "region", "tier"],
)
_burn_rate_1h = get_or_create_gauge(
    "baldur_error_budget_burn_rate_1h",
    "Error budget burn rate over 1 hour window",
    ["slo_name"],
)
_burn_rate_6h = get_or_create_gauge(
    "baldur_error_budget_burn_rate_6h",
    "Error budget burn rate over 6 hour window",
    ["slo_name"],
)
_deployment_freeze_status = get_or_create_gauge(
    "baldur_deployment_freeze_status",
    "Deployment freeze status (0=proceed, 1=caution, 2=warning, 3=freeze_recommended)",
    [],
)
_freeze_decision_total = get_or_create_counter(
    "baldur_freeze_decision_total",
    "Total freeze-related decisions",
    ["decision_type"],
)
_active_override_gauge = get_or_create_gauge(
    "baldur_deployment_active_override",
    "Whether there is an active deployment override (0=no, 1=yes)",
    [],
)

# Heartbeat
_heartbeat_timestamp = get_or_create_gauge(
    "baldur_heartbeat_timestamp_seconds",
    "Last heartbeat timestamp in seconds since epoch",
    ["component"],
)
_heartbeat_count = get_or_create_counter(
    "baldur_heartbeat_total",
    "Total heartbeat emissions",
    ["component"],
)
_override_escalation_total = get_or_create_counter(
    "baldur_override_escalation_total",
    "Total override escalation alerts sent",
    ["override_type"],
)
_recovery_alert_total = get_or_create_counter(
    "baldur_recovery_alert_total",
    "Total recovery alerts sent",
    ["component"],
)

# Fail-Safe
_failsafe_triggered_total = get_or_create_counter(
    "baldur_failsafe_triggered_total",
    "Number of times fail-safe mode was activated",
    ["component"],
)
_failsafe_mode_active = get_or_create_gauge(
    "baldur_failsafe_mode_active",
    "Whether fail-safe mode is currently active (1=yes, 0=no)",
    ["component"],
)

# X-Test
_xtest_cross_region_denied_total = get_or_create_counter(
    "baldur_xtest_cross_region_denied_total",
    "Total cross-region X-Test requests denied (region mismatch)",
    ["current_region", "target_region"],
)
_xtest_global_scope_requests_total = get_or_create_counter(
    "baldur_xtest_global_scope_requests_total",
    "Total GLOBAL scope X-Test API requests",
    ["endpoint_pattern", "region", "result"],
)


# =============================================================================
# DLQ Recording Functions (delegate to domain recorder)
# =============================================================================


def record_dlq_item_created(domain: str, failure_type: str) -> None:
    """
    Record that a new DLQ item was created.

    Args:
        domain: Business domain (payment, point, inventory, etc.)
        failure_type: Specific failure type (PG_TIMEOUT, AMOUNT_MISMATCH, etc.)
    """
    try:
        from baldur.metrics.prometheus import get_metrics

        domain = resolve_domain_label(domain)
        get_metrics().dlq.record_item_created(domain, failure_type)
    except Exception as e:
        logger.warning("metrics.record_dlq_creation_failed", error=e)


def record_sla_breach(domain: str) -> None:
    """
    Record an SLA breach event.

    Args:
        domain: Business domain where breach occurred
    """
    try:
        domain = resolve_domain_label(domain)
        _sla_breach_total.labels(domain=domain).inc()
        logger.info(
            "metrics.sla_breach_recorded",
            healing_domain=domain,
        )
    except Exception as e:
        logger.warning("metrics.record_sla_breach_failed", error=e)


# =============================================================================
# Retry Recording Functions (delegate to domain recorder)
# =============================================================================


def record_retry_attempt(domain: str, attempt_count: int, outcome: str) -> None:
    """
    Record a retry attempt outcome.

    Args:
        domain: Business domain
        attempt_count: Number of attempts made
        outcome: Result (success, failure, exhausted)
    """
    try:
        from baldur.metrics.prometheus import get_metrics

        domain = resolve_domain_label(domain)
        get_metrics().retry.record_attempt(domain, attempt_count, outcome)
    except Exception as e:
        logger.warning("metrics.record_retry_metric_failed", error=e)


# =============================================================================
# Recovery Recording Functions
# =============================================================================


def record_recovery_time(
    domain: str,
    resolution_type: str,
    created_at: datetime,
    resolved_at: datetime,
) -> None:
    """
    Record time from failure to resolution.

    Args:
        domain: Business domain
        resolution_type: How it was resolved (auto_replay, manual_fix, etc.)
        created_at: When the failure was created
        resolved_at: When it was resolved
    """
    try:
        domain = resolve_domain_label(domain)
        duration = (resolved_at - created_at).total_seconds()
        _recovery_time_seconds.labels(
            domain=domain, resolution_type=resolution_type
        ).observe(duration)
        logger.debug(
            "metrics.recovery_time_recorded",
            healing_domain=domain,
            resolution_type=resolution_type,
            duration=duration,
        )
    except Exception as e:
        logger.warning("metrics.record_recovery_time_failed", error=e)


# =============================================================================
# Circuit Breaker Recording Functions (delegate to domain recorder)
# =============================================================================


def record_circuit_breaker_state_change(
    service: str,
    from_state: str,
    to_state: str,
) -> None:
    """
    Record a circuit breaker state transition.

    Args:
        service: Service name (e.g., external_gateway)
        from_state: Previous state
        to_state: New state
    """
    try:
        from baldur.core.cb_namespace import (
            parse_composite_cb_name,
        )
        from baldur.metrics.prometheus import get_metrics

        base_service, cell_id = parse_composite_cb_name(service)
        get_metrics().circuit_breaker.record_state_change(
            base_service, from_state, to_state, cell_id
        )
    except Exception as e:
        logger.warning("metrics.record_circuit_breaker_failed", error=e)


def record_circuit_breaker_open_duration(service: str, duration_seconds: float) -> None:
    """
    Record how long a circuit breaker was in open state.

    Args:
        service: Service name
        duration_seconds: Time spent in open state
    """
    try:
        from baldur.metrics.prometheus import get_metrics

        get_metrics().circuit_breaker.record_open_duration(service, duration_seconds)
    except Exception as e:
        logger.warning("metrics.record_cb_duration_failed", error=e)


# =============================================================================
# L2 Storage Recording Functions
# =============================================================================


def record_l2_timeout(adapter_type: str, operation: str) -> None:
    """Record L2 timeout occurrence."""
    try:
        _l2_timeout_total.labels(adapter_type=adapter_type, operation=operation).inc()
        _l2_connection_status.labels(adapter_type=adapter_type).set(0)
    except Exception as e:
        logger.warning("metrics.record_timeout_failed", error=e)


def record_l2_sync_failure(adapter_type: str, operation: str) -> None:
    """Record L2 sync failure."""
    try:
        _l2_sync_failure_total.labels(
            adapter_type=adapter_type, operation=operation
        ).inc()
    except Exception as e:
        logger.warning("metrics.record_sync_failure_failed", error=e)


def record_l2_latency(adapter_type: str, latency_seconds: float) -> None:
    """Record L2 operation latency."""
    try:
        _l2_latency_seconds.labels(adapter_type=adapter_type).observe(latency_seconds)
        _l2_connection_status.labels(adapter_type=adapter_type).set(1)
    except Exception as e:
        logger.warning("metrics.record_latency_failed", error=e)


# =============================================================================
# Replay Recording Functions (delegate to domain recorder)
# =============================================================================


def record_replay_attempt(domain: str, replay_type: str, success: bool) -> None:
    """
    Record a replay attempt.

    Args:
        domain: Business domain
        replay_type: Type of replay (single, batch, conditional)
        success: Whether replay succeeded
    """
    try:
        from baldur.metrics.prometheus import get_metrics

        domain = resolve_domain_label(domain)
        get_metrics().replay.record_attempt(domain, replay_type, success)
    except Exception as e:
        logger.warning("metrics.record_replay_metric_failed", error=e)


# =============================================================================
# Error Budget Recording Functions
# =============================================================================


def record_error_budget_status(
    slo_name: str,
    remaining_percent: float,
    remaining_minutes: float,
    burn_rate_1h_value: float,
    burn_rate_6h_value: float,
    region: str = "",
    tier: str = "",
) -> None:
    """
    Record Error Budget status metrics.

    Args:
        slo_name: SLO name (e.g., "availability")
        remaining_percent: Budget remaining percentage (0-100)
        remaining_minutes: Budget remaining in minutes
        burn_rate_1h_value: 1-hour burn rate
        burn_rate_6h_value: 6-hour burn rate
        region: Region identifier (empty string for global)
        tier: Tier identifier (empty string for unspecified)
    """
    try:
        is_synthetic = TestModeContext.get_synthetic_label_value()
        _error_budget_remaining_percent.labels(
            slo_name=slo_name,
            is_synthetic=is_synthetic,
            region=region,
            tier=tier,
        ).set(remaining_percent)
        _error_budget_remaining_minutes.labels(
            slo_name=slo_name,
            is_synthetic=is_synthetic,
            region=region,
            tier=tier,
        ).set(remaining_minutes)
        _burn_rate_1h.labels(slo_name=slo_name).set(burn_rate_1h_value)
        _burn_rate_6h.labels(slo_name=slo_name).set(burn_rate_6h_value)
        logger.debug(
            "metrics.error_budget_recorded",
            slo_name=slo_name,
            remaining_percent=remaining_percent,
            burn_rate_1h_value=burn_rate_1h_value,
            is_synthetic=is_synthetic,
            target_region=region,
            tier=tier,
        )
    except Exception as e:
        logger.warning("metrics.record_error_budget_failed", error=e)


def record_deployment_freeze_status(status: str) -> None:
    """
    Record deployment freeze status.

    Args:
        status: Freeze status (proceed, caution, warning, freeze_recommended)
    """
    try:
        status_mapping = {
            "proceed": 0,
            "caution": 1,
            "warning": 2,
            "freeze_recommended": 3,
        }
        status_value = status_mapping.get(status, 0)
        _deployment_freeze_status.set(status_value)
        logger.debug(
            "metrics.deployment_freeze_status",
            metric_status=status,
            status_value=status_value,
        )
    except Exception as e:
        logger.warning("metrics.record_freeze_status_failed", error=e)


def record_freeze_decision(decision_type: str) -> None:
    """
    Record a freeze-related decision.

    Args:
        decision_type: Type of decision (freeze_acknowledged, override_approved, freeze_lifted)
    """
    try:
        _freeze_decision_total.labels(decision_type=decision_type).inc()
        logger.info(
            "metrics.freeze_decision_recorded",
            decision_type=decision_type,
        )
    except Exception as e:
        logger.warning("metrics.record_freeze_decision_failed", error=e)


def record_active_override(has_override: bool) -> None:
    """
    Record whether there is an active deployment override.

    Args:
        has_override: Whether an override is active
    """
    try:
        _active_override_gauge.set(1 if has_override else 0)
        logger.debug(
            "metrics.active_override",
            has_override=has_override,
        )
    except Exception as e:
        logger.warning("metrics.record_active_override_failed", error=e)


# =============================================================================
# Fail-Safe Recording Functions
# =============================================================================


def record_failsafe_triggered(component: str) -> None:
    """
    Record that fail-safe mode was triggered.

    This metric is CRITICAL for detecting "silent failures".
    It should trigger alerts in Prometheus/Grafana.

    Args:
        component: The component that triggered fail-safe (e.g., "error_budget")
    """
    try:
        _failsafe_triggered_total.labels(component=component).inc()
        _failsafe_mode_active.labels(component=component).set(1)
        logger.critical(
            "metrics.fail_safe_triggered_alerting",
            monitored_component=component,
        )
    except Exception as e:
        logger.exception("metrics.record_fail_safe_failed", error=e)


def record_failsafe_recovered(component: str) -> None:
    """
    Record that fail-safe mode has been recovered.

    Args:
        component: The component that recovered
    """
    try:
        _failsafe_mode_active.labels(component=component).set(0)
        logger.info(
            "metrics.fail_safe_recovered",
            monitored_component=component,
        )
    except Exception as e:
        logger.warning("metrics.record_fail_safe_failed", error=e)


# =============================================================================
# Heartbeat Functions (Dead Man's Snitch)
# =============================================================================


def emit_heartbeat(component: str = "error_budget") -> None:
    """
    Emit a heartbeat signal indicating the system is alive.

    This should be called periodically (default: every 60 seconds).
    If this metric stops being updated, it indicates the service is dead.

    Args:
        component: The component emitting the heartbeat

    Prometheus Alert Rule:
        - alert: BaldurServiceDead
          expr: time() - baldur_heartbeat_timestamp_seconds > 120
          for: 0m
          labels:
            severity: critical
    """
    try:
        current_time = time.time()
        _heartbeat_timestamp.labels(component=component).set(current_time)
        _heartbeat_count.labels(component=component).inc()
        logger.debug(
            "metrics.heartbeat_emitted",
            monitored_component=component,
            current_time=current_time,
        )
    except Exception as e:
        logger.exception("metrics.emit_heartbeat_failed", error=e)


def record_override_escalation(override_type: str) -> None:
    """
    Record that an override escalation alert was sent.

    Args:
        override_type: Type of override (hotfix, security_patch, etc.)
    """
    try:
        _override_escalation_total.labels(override_type=override_type).inc()
        logger.info(
            "metrics.override_escalation_recorded",
            override_type=override_type,
        )
    except Exception as e:
        logger.warning("metrics.record_override_escalation_failed", error=e)


def record_recovery_alert(component: str) -> None:
    """
    Record that a recovery alert was sent.

    Args:
        component: The component that recovered
    """
    try:
        _recovery_alert_total.labels(component=component).inc()
        logger.info(
            "metrics.recovery_alert_recorded",
            monitored_component=component,
        )
    except Exception as e:
        logger.warning("metrics.record_recovery_alert_failed", error=e)


# =============================================================================
# X-Test Regional Boundary Recording Functions
# =============================================================================


def record_xtest_cross_region_denied(
    current_region: str,
    target_region: str,
) -> None:
    """
    Record a cross-region X-Test request denial.

    Args:
        current_region: Current cluster region (e.g., 'seoul')
        target_region: Requested target region from X-Region header
    """
    try:
        _xtest_cross_region_denied_total.labels(
            current_region=current_region,
            target_region=target_region,
        ).inc()
        logger.warning(
            "metrics.cross_region_test_denied",
            current_region=current_region,
            target_region=target_region,
        )
    except Exception as e:
        logger.warning("metrics.record_cross_region_failed", error=e)


def record_xtest_global_scope_request(
    endpoint_pattern: str,
    region: str,
    result: str,
) -> None:
    """
    Record a GLOBAL scope X-Test API request.

    Args:
        endpoint_pattern: Matched GLOBAL scope pattern (e.g., 'emergency', 'isolation')
        region: Current or target region
        result: Request result ('allowed', 'denied_no_header', 'denied_mismatch')
    """
    try:
        _xtest_global_scope_requests_total.labels(
            endpoint_pattern=endpoint_pattern,
            region=region,
            result=result,
        ).inc()
        logger.debug(
            "metrics.global_scope_request",
            endpoint_pattern=endpoint_pattern,
            target_region=region,
            xtest_result=result,
        )
    except Exception as e:
        logger.warning("metrics.record_global_scope_failed", error=e)
