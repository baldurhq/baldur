"""OSS-side thin wrappers for PRO audit helpers (518 D4/D7).

Provides a single, stable import target for OSS callsites that need to log
audit entries. When ``baldur_pro.services.audit`` is installed, each wrapper
delegates to the corresponding PRO function (preserving WAL writes,
request-scoped buffering, and adapter routing). When PRO is not installed,
each wrapper silently no-ops and returns ``None``.

Each wrapper accepts ``*args, **kwargs`` and forwards them verbatim — the
caller's exact argument shape (positional or keyword) is preserved into the
PRO call. The matching PRO signature is listed in each wrapper's docstring;
consult ``src/baldur_pro/services/audit/`` for parameter types and defaults.

Fail-open semantics
-------------------
All wrappers fail-open: if the resolved PRO function raises any exception
(WAL unreachable, adapter misconfigured, etc.), the wrapper logs at DEBUG
and returns ``None``. Audit logging is best-effort and MUST NOT crash
business logic — this matches the pre-518 ``try/except`` pattern that
every callsite wore individually.

Background
----------
This module supersedes the 514 ``try/except ImportError`` band-aid and the
516 D6 ``ProviderRegistry.audit.get().log(AuditEntry)`` recipe. PRO helpers
carry rich semantics (``_write_to_wal`` + ``_try_add_to_buffer(request, ...)``
+ adapter fallback) that cannot be expressed by an ``AuditEntry`` blob without
losing the "callsite changes only the import line" stability promise.

Test isolation
--------------
The ``_pro`` / ``_resolved`` module globals cache the PRO module resolution.
Tests that swap PRO presence (or pop ``baldur_pro.services.audit`` from
``sys.modules``) MUST reset these globals via the ``reset_audit_helpers``
fixture in ``tests/conftest.py``; otherwise the cached reference points at
the prior module object.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

_pro: Any = None
_resolved: bool = False


def _get_pro() -> Any:
    """Return the cached :mod:`baldur_pro.services.audit` module or ``None``."""
    global _pro, _resolved
    if not _resolved:
        try:
            import baldur_pro.services.audit as _m

            _pro = _m
        except ImportError:
            _pro = None
        _resolved = True
    return _pro


def _safe_delegate(func_name: str, *args: Any, **kwargs: Any) -> Any:
    """Delegate to a PRO audit function, fail-open on any exception.

    Log level: the ``_failed`` event is emitted at WARNING, never lower —
    audit/compliance recording failures are GDPR/SOC2-essential. The operator
    must be aware that an audit write was missed, even though the caller's
    primary operation continues.
    """
    p = _get_pro()
    if p is None:
        return None
    try:
        return getattr(p, func_name)(*args, **kwargs)
    except Exception:
        logger.warning("audit.helper_failed", function=func_name, exc_info=True)
        return None


# ============================================================
# Circuit Breaker & Governance
# ============================================================


def log_cb_state_change_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_cb_state_change_audit(cb_name, old_state, new_state, reason=None, request=None, actor_id=None, actor_type=None)."""
    return _safe_delegate("log_cb_state_change_audit", *args, **kwargs)


def log_governance_blocked_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_governance_blocked_audit(action, block_reason, details=None, request=None)."""
    return _safe_delegate("log_governance_blocked_audit", *args, **kwargs)


def log_rate_limited_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_rate_limited_audit(client_ip, endpoint, limit_type, request=None)."""
    return _safe_delegate("log_rate_limited_audit", *args, **kwargs)


def log_pool_cb_rejection_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_pool_cb_rejection_audit(pool_name, current_utilization, threshold, decision_source='cached_pool_status', request=None)."""
    return _safe_delegate("log_pool_cb_rejection_audit", *args, **kwargs)


def log_governance_blocked_cb_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_governance_blocked_cb_audit(service_id, action, block_reason, ...)."""
    return _safe_delegate("log_governance_blocked_cb_audit", *args, **kwargs)


# ============================================================
# Chaos Experiment & Emergency Mode
# ============================================================


def log_kill_switch_override_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_kill_switch_override_audit(service_name, action, reason='', controlled_by_id=None, request=None)."""
    return _safe_delegate("log_kill_switch_override_audit", *args, **kwargs)


def log_panic_threshold_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_panic_threshold_audit(open_rate, threshold, open_count, total_count, ...)."""
    return _safe_delegate("log_panic_threshold_audit", *args, **kwargs)


def log_freeze_mode_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_freeze_mode_audit(active, reason='', activated_by='system', previous_state=None, emergency_level=None, request=None)."""
    return _safe_delegate("log_freeze_mode_audit", *args, **kwargs)


def log_chaos_experiment_audit(*args: Any, **kwargs: Any) -> str | None:
    """PRO: log_chaos_experiment_audit(experiment_id, event_type, ...) -> str."""
    return _safe_delegate("log_chaos_experiment_audit", *args, **kwargs)


def log_emergency_mode_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_emergency_mode_audit(action, level, is_active, ...)."""
    return _safe_delegate("log_emergency_mode_audit", *args, **kwargs)


def log_error_budget_blocked_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_error_budget_blocked_audit(action, gate_status, ...)."""
    return _safe_delegate("log_error_budget_blocked_audit", *args, **kwargs)


def log_error_budget_warning_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_error_budget_warning_audit(budget_percent, threshold_percent, request=None)."""
    return _safe_delegate("log_error_budget_warning_audit", *args, **kwargs)


def log_error_budget_recovered_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_error_budget_recovered_audit(budget_percent, threshold_percent, request=None)."""
    return _safe_delegate("log_error_budget_recovered_audit", *args, **kwargs)


# ============================================================
# Compliance, Security & FinOps
# ============================================================


def log_security_violation_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_security_violation_audit(violation_type, action, target, result, ...)."""
    return _safe_delegate("log_security_violation_audit", *args, **kwargs)


def log_region_isolation_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_region_isolation_audit(region, action, result, ...)."""
    return _safe_delegate("log_region_isolation_audit", *args, **kwargs)


def log_compliance_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_compliance_audit(service_name, standard, ...)."""
    return _safe_delegate("log_compliance_audit", *args, **kwargs)


def log_blast_radius_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_blast_radius_audit(experiment_id, blast_radius, target_service, action, ...)."""
    return _safe_delegate("log_blast_radius_audit", *args, **kwargs)


def log_finops_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_finops_audit(service_name, alert_type, ...)."""
    return _safe_delegate("log_finops_audit", *args, **kwargs)


def log_data_access_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_data_access_audit(path, method, ...)."""
    return _safe_delegate("log_data_access_audit", *args, **kwargs)


# ============================================================
# Daily Report
# ============================================================


def log_daily_report_send_failed_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_daily_report_send_failed_audit(channel, error, request=None)."""
    return _safe_delegate("log_daily_report_send_failed_audit", *args, **kwargs)


def log_daily_report_generated_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_daily_report_generated_audit(report_date, channels, request=None)."""
    return _safe_delegate("log_daily_report_generated_audit", *args, **kwargs)


# ============================================================
# DLQ
# ============================================================


def log_dlq_store_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_dlq_store_audit(dlq_id, domain, failure_type, error_message=None, request=None)."""
    return _safe_delegate("log_dlq_store_audit", *args, **kwargs)


def log_dlq_replay_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_dlq_replay_audit(dlq_id, domain, success, actor_id=None, error_message=None, request=None)."""
    return _safe_delegate("log_dlq_replay_audit", *args, **kwargs)


def log_dlq_replay_blocked_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_dlq_replay_blocked_audit(domain, reason, service_name, trigger, details=None)."""
    return _safe_delegate("log_dlq_replay_blocked_audit", *args, **kwargs)


def log_dlq_force_redrive_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_dlq_force_redrive_audit(dlq_id, domain, actor_id=None, reason='', ticket_url=None, previous_total_retries=None, request=None)."""
    return _safe_delegate("log_dlq_force_redrive_audit", *args, **kwargs)


def log_dlq_compress_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_dlq_compress_audit(source_count, summary_count, details)."""
    return _safe_delegate("log_dlq_compress_audit", *args, **kwargs)


# ============================================================
# Forecaster
# ============================================================


def log_proactive_action_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_proactive_action_audit(metric_name, parameter, spike_type, current_value, suggested_value, confidence, is_dry_run, request=None)."""
    return _safe_delegate("log_proactive_action_audit", *args, **kwargs)


# ============================================================
# Learning
# ============================================================


def log_parameter_blacklisted_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_parameter_blacklisted_audit(module, parameter, reason, blocked_values=None, registered_by='system', request=None)."""
    return _safe_delegate("log_parameter_blacklisted_audit", *args, **kwargs)


def log_pattern_detected_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_pattern_detected_audit(pattern_type, name, confidence, request=None)."""
    return _safe_delegate("log_pattern_detected_audit", *args, **kwargs)


def log_manual_only_mode_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_manual_only_mode_audit(module, enabled, changed_by='system', request=None)."""
    return _safe_delegate("log_manual_only_mode_audit", *args, **kwargs)


# ============================================================
# Pool Watchdog
# ============================================================


def log_pool_leak_closed_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_pool_leak_closed_audit(pool_name, closed_count, leaked_connection_ids=None, request=None)."""
    return _safe_delegate("log_pool_leak_closed_audit", *args, **kwargs)


def log_pool_expand_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_pool_expand_audit(pool_name, expanded_by, new_size, reason='exhaustion', request=None)."""
    return _safe_delegate("log_pool_expand_audit", *args, **kwargs)


# ============================================================
# Retry, Rollback & System Control
# ============================================================


def log_retry_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_retry_audit(domain, attempt, max_attempts, success, ...)."""
    return _safe_delegate("log_retry_audit", *args, **kwargs)


def log_system_control_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_system_control_audit(action, actor, old_state=None, new_state=None, reason=None, request=None)."""
    return _safe_delegate("log_system_control_audit", *args, **kwargs)


def log_rollback_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_rollback_audit(request_id, service_name, state, triggered_by, ...)."""
    return _safe_delegate("log_rollback_audit", *args, **kwargs)


# ============================================================
# Saga
# ============================================================


def log_saga_timeout_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_saga_timeout_audit(saga_name, instance_id, timeout_seconds=None, request=None)."""
    return _safe_delegate("log_saga_timeout_audit", *args, **kwargs)


def log_saga_compensation_failed_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_saga_compensation_failed_audit(saga_name, instance_id, failed_steps=None, error_message=None, request=None)."""
    return _safe_delegate("log_saga_compensation_failed_audit", *args, **kwargs)


# ============================================================
# Storage & Tasks
# ============================================================


def log_storage_failure_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_storage_failure_audit(storage_type, adapter_type, operation, service_name, error_type, error_message, consecutive_failures, trace_id=None, request=None)."""
    return _safe_delegate("log_storage_failure_audit", *args, **kwargs)


def log_storage_recovery_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_storage_recovery_audit(storage_type, adapter_type, total_failures, downtime_seconds=None, trace_id=None, request=None)."""
    return _safe_delegate("log_storage_recovery_audit", *args, **kwargs)


def log_drift_reconciliation_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_drift_reconciliation_audit(adapter_type, total_checked, reconciled, l1_wins, l2_wins, error_count, trace_id=None, request=None)."""
    return _safe_delegate("log_drift_reconciliation_audit", *args, **kwargs)


def log_config_apply_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_config_apply_audit(pending_id=None, config_key=None, ...)."""
    return _safe_delegate("log_config_apply_audit", *args, **kwargs)


def log_chaos_scheduler_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_chaos_scheduler_audit(experiment_id=None, experiment_name=None, ...)."""
    return _safe_delegate("log_chaos_scheduler_audit", *args, **kwargs)


def log_governance_task_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_governance_task_audit(action='expiry_check', ...)."""
    return _safe_delegate("log_governance_task_audit", *args, **kwargs)


def log_traffic_aware_replay_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_traffic_aware_replay_audit(domain=None, status='completed', ...)."""
    return _safe_delegate("log_traffic_aware_replay_audit", *args, **kwargs)


def log_drift_detection_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_drift_detection_audit(check_type='sla_drift', status='completed', ...)."""
    return _safe_delegate("log_drift_detection_audit", *args, **kwargs)


# ============================================================
# X-Test-Mode
# ============================================================


def log_xtest_operation_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_xtest_operation_audit(session_id, action, component, details, result, ...)."""
    return _safe_delegate("log_xtest_operation_audit", *args, **kwargs)


def log_xtest_scenario_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_xtest_scenario_audit(scenario_id, scenario_name, service_name, status, steps_total, steps_completed, errors, duration_ms, ...)."""
    return _safe_delegate("log_xtest_scenario_audit", *args, **kwargs)


def log_xtest_session_start_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_xtest_session_start_audit(session_id, user='anonymous', metadata=None)."""
    return _safe_delegate("log_xtest_session_start_audit", *args, **kwargs)


def log_xtest_session_end_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_xtest_session_end_audit(session_id, operations_count, scenarios_count, duration_seconds, user='anonymous', summary=None)."""
    return _safe_delegate("log_xtest_session_end_audit", *args, **kwargs)


def log_xtest_injection_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_xtest_injection_audit(session_id, component, injection_type, count, target_ids, user='anonymous')."""
    return _safe_delegate("log_xtest_injection_audit", *args, **kwargs)


def log_xtest_cleanup_audit(*args: Any, **kwargs: Any) -> int | None:
    """PRO: log_xtest_cleanup_audit(session_id, component, cleaned_count, cleaned_ids, user='anonymous')."""
    return _safe_delegate("log_xtest_cleanup_audit", *args, **kwargs)


__all__ = [
    "log_blast_radius_audit",
    "log_cb_state_change_audit",
    "log_chaos_experiment_audit",
    "log_chaos_scheduler_audit",
    "log_compliance_audit",
    "log_config_apply_audit",
    "log_daily_report_generated_audit",
    "log_daily_report_send_failed_audit",
    "log_data_access_audit",
    "log_dlq_compress_audit",
    "log_dlq_force_redrive_audit",
    "log_dlq_replay_audit",
    "log_dlq_replay_blocked_audit",
    "log_dlq_store_audit",
    "log_drift_detection_audit",
    "log_drift_reconciliation_audit",
    "log_emergency_mode_audit",
    "log_error_budget_blocked_audit",
    "log_error_budget_recovered_audit",
    "log_error_budget_warning_audit",
    "log_finops_audit",
    "log_freeze_mode_audit",
    "log_governance_blocked_audit",
    "log_governance_blocked_cb_audit",
    "log_governance_task_audit",
    "log_kill_switch_override_audit",
    "log_manual_only_mode_audit",
    "log_panic_threshold_audit",
    "log_parameter_blacklisted_audit",
    "log_pattern_detected_audit",
    "log_pool_cb_rejection_audit",
    "log_pool_expand_audit",
    "log_pool_leak_closed_audit",
    "log_proactive_action_audit",
    "log_rate_limited_audit",
    "log_region_isolation_audit",
    "log_retry_audit",
    "log_rollback_audit",
    "log_saga_compensation_failed_audit",
    "log_saga_timeout_audit",
    "log_security_violation_audit",
    "log_storage_failure_audit",
    "log_storage_recovery_audit",
    "log_system_control_audit",
    "log_traffic_aware_replay_audit",
    "log_xtest_cleanup_audit",
    "log_xtest_injection_audit",
    "log_xtest_operation_audit",
    "log_xtest_scenario_audit",
    "log_xtest_session_end_audit",
    "log_xtest_session_start_audit",
]
