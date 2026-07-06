"""
Baldur Observability Metrics.

Prometheus metrics for monitoring the L3 Baldur layer.
Provides comprehensive visibility into DLQ, retry, recovery, and circuit breaker operations.

This package has been refactored from a single 1,247-line file into:
- definitions.py: All metric definitions (Counter, Gauge, Histogram)
- recorders.py: record_* functions for event recording
- updaters.py: update_* functions, context managers, decorators

All exports are maintained for backward compatibility.

DLQ, 재시도, 복구, Circuit Breaker 운영에 대한 포괄적인 관측성을 제공합니다.
"""

from __future__ import annotations

# Domain Registration (re-exported from metrics.registry)
from baldur.metrics.registry import get_registered_domains, register_domain

# Metric Definitions
from .definitions import (  # Retry; L2 Storage; Heartbeat; Fail-Safe
    baldur_heartbeat_count,
    baldur_heartbeat_timestamp,
    drift_reconciliation_total,
    failsafe_mode_active,
    failsafe_triggered_total,
    l2_connection_status,
    l2_latency_seconds,
    l2_sync_failure_total,
    l2_timeout_total,
    override_escalation_total,
    recovery_alert_total,
    retry_attempts_histogram,
    shadow_log_unsynced_count,
)

# Recorders
from .recorders import (
    emit_heartbeat,
    record_active_override,
    record_circuit_breaker_open_duration,
    record_circuit_breaker_state_change,
    record_deployment_freeze_status,
    record_dlq_item_created,
    record_error_budget_status,
    record_failsafe_recovered,
    record_failsafe_triggered,
    record_freeze_decision,
    record_l2_latency,
    record_l2_sync_failure,
    record_l2_timeout,
    record_override_escalation,
    record_recovery_alert,
    record_recovery_time,
    record_replay_attempt,
    record_retry_attempt,
    record_sla_breach,
)

# Updaters
from .updaters import (
    collect_all_metrics,
    track_recovery_time,
    update_circuit_breaker_gauges,
    update_dlq_pending_gauges,
    update_dlq_status_gauges,
    update_retry_success_rates,
    update_shadow_log_metrics,
)

__all__ = [
    # Retry Metrics
    "retry_attempts_histogram",
    # L2 Storage Metrics
    "l2_timeout_total",
    "l2_sync_failure_total",
    "l2_latency_seconds",
    "l2_connection_status",
    "shadow_log_unsynced_count",
    "drift_reconciliation_total",
    # Heartbeat Metrics
    "baldur_heartbeat_timestamp",
    "baldur_heartbeat_count",
    "override_escalation_total",
    "recovery_alert_total",
    # Fail-Safe Metrics
    "failsafe_triggered_total",
    "failsafe_mode_active",
    # Recording Functions
    "record_dlq_item_created",
    "record_sla_breach",
    "record_retry_attempt",
    "record_recovery_time",
    "record_circuit_breaker_state_change",
    "record_circuit_breaker_open_duration",
    "record_l2_timeout",
    "record_l2_sync_failure",
    "record_l2_latency",
    "record_replay_attempt",
    "record_error_budget_status",
    "record_deployment_freeze_status",
    "record_freeze_decision",
    "record_active_override",
    "record_failsafe_triggered",
    "record_failsafe_recovered",
    "emit_heartbeat",
    "record_override_escalation",
    "record_recovery_alert",
    # Update Functions
    "update_shadow_log_metrics",
    "update_dlq_pending_gauges",
    "update_dlq_status_gauges",
    "update_circuit_breaker_gauges",
    "update_retry_success_rates",
    "track_recovery_time",
    "collect_all_metrics",
    # Domain Registration
    "get_registered_domains",
    "register_domain",
]
