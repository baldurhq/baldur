"""
Celery Tasks for Baldur System.

These tasks provide background processing for:
- Circuit breaker state management
- DLQ replay operations
- Metrics collection
- SLA monitoring
- Cleanup operations

Usage:
    Add these tasks to your Celery beat schedule:

    CELERY_BEAT_SCHEDULE = {
        'check-circuit-breaker-recovery': {
            'task': 'baldur.celery_tasks.check_circuit_breaker_recovery',
            'schedule': 60.0,  # Every minute
        },
        'expire-manual-overrides': {
            'task': 'baldur.celery_tasks.expire_manual_overrides',
            'schedule': 300.0,  # Every 5 minutes
        },
        'collect-baldur-metrics': {
            'task': 'baldur.adapters.celery.tasks.collect_baldur_metrics',
            'schedule': 60.0,  # Every minute
        },
        'check-sla-breaches': {
            'task': 'baldur.adapters.celery.tasks.check_and_report_sla_breaches',
            'schedule': 300.0,  # Every 5 minutes
        },
        'cleanup-dlq-entries': {
            'task': 'baldur.adapters.celery.tasks.cleanup_resolved_dlq_entries',
            'schedule': 86400.0,  # Daily
        },
        'emit-baldur-heartbeat': {
            'task': 'baldur.adapters.celery.tasks.emit_baldur_heartbeat',
            'schedule': 60.0,  # Every minute
        },
    }
"""

from __future__ import annotations

# ============================================================
# Circuit Breaker Tasks (consolidated into celery_tasks/)
# ============================================================
from baldur.celery_tasks.circuit_breaker_tasks import (
    check_circuit_breaker_recovery,
    collect_cb_open_snapshot,
    expire_manual_overrides,
    force_close_circuit_breaker,
    force_open_circuit_breaker,
    send_cb_close_notification,
    send_cb_open_notification,
)
from baldur.celery_tasks.dlq_tasks import conditional_replay_on_circuit_close

# ============================================================
# Cell Evacuation Tasks
# ============================================================
from .cell_evacuation import (
    notify_cell_blast_radius,
    notify_cell_isolation,
    notify_cell_restoration,
)

# ============================================================
# DLQ Replay Tasks
# ============================================================
from .dlq_replay import (
    cleanup_resolved_dlq_entries,
    replay_batch_by_domain,
    replay_single_dlq_entry,
)

# ============================================================
# Metrics & Monitoring Tasks
# ============================================================
from .monitoring import (
    check_and_report_sla_breaches,
    collect_baldur_metrics,
    emit_baldur_heartbeat,
    notify_failsafe_recovery,
)

# ============================================================
# Async Persistence Tasks
# ============================================================
from .persistence import (
    async_persist_batch,
    async_persist_dlq_entry,
    link_audit_to_dlq,
)

# ============================================================
# Postmortem Tasks
# ============================================================
from .postmortem import (
    check_stale_incident_groups,
    close_incident_group,
    flush_aggregated_notifications,
    process_individual_postmortem,
)

# Runbook tasks moved to baldur_pro.services.runbook.celery_tasks
# (599 D10 — registered by register_pro_services on import).
# ============================================================
# SLA Notification Tasks
# ============================================================
from .sla_notification import send_sla_notification

# ============================================================
# Public API
# ============================================================
__all__ = [
    # Persistence
    "async_persist_dlq_entry",
    "async_persist_batch",
    "link_audit_to_dlq",
    # Circuit Breaker
    "conditional_replay_on_circuit_close",
    "check_circuit_breaker_recovery",
    "force_open_circuit_breaker",
    "force_close_circuit_breaker",
    "expire_manual_overrides",
    "send_cb_open_notification",
    "send_cb_close_notification",
    "collect_cb_open_snapshot",
    # DLQ Replay
    "replay_single_dlq_entry",
    "replay_batch_by_domain",
    "cleanup_resolved_dlq_entries",
    # Monitoring
    "collect_baldur_metrics",
    "check_and_report_sla_breaches",
    "emit_baldur_heartbeat",
    "notify_failsafe_recovery",
    # Cell Evacuation
    "notify_cell_blast_radius",
    "notify_cell_isolation",
    "notify_cell_restoration",
    # SLA Notification
    "send_sla_notification",
    # Postmortem
    "close_incident_group",
    "flush_aggregated_notifications",
    "check_stale_incident_groups",
    "process_individual_postmortem",
]
