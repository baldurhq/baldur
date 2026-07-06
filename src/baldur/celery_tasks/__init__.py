"""
Baldur Celery Tasks

All Celery tasks for the baldur package are defined here.
This module can be autodiscovered by Celery and used in any Django project.

Usage in your Django project's celery.py:
    app.autodiscover_tasks(['baldur.celery_tasks'])

Or simply import all tasks in your host application's tasks.py:
    from baldur.celery_tasks import *  # noqa

Tasks are grouped by domain:
- circuit_breaker_tasks: Circuit breaker management
- dlq_tasks: DLQ replay operations
- chaos_tasks: Chaos engineering safety
- metrics_tasks: Observability and SLA monitoring
- drift_detection_tasks: SLA drift detection
- audit_flush_tasks: Redis audit-buffer drain (gated)

Status: Internal
"""

from baldur.celery_tasks.audit_flush_tasks import (
    apply_audit_buffer_safety_ltrim,
    flush_redis_audit_buffer,
    recover_orphaned_processing_queues,
)
from baldur.celery_tasks.chaos_tasks import (
    check_recovery_monitoring_experiments,
)
from baldur.celery_tasks.circuit_breaker_tasks import (
    check_circuit_breaker_recovery,
    expire_manual_overrides,
    force_close_circuit_breaker,
    force_open_circuit_breaker,
)
from baldur.celery_tasks.dlq_tasks import (
    cleanup_resolved_dlq_entries,
    conditional_replay_on_circuit_close,
    replay_batch_by_domain,
    replay_batch_by_failure_type,
    replay_single_dlq_entry,
)
from baldur.celery_tasks.drift_detection_tasks import (
    check_sla_drift,
    cleanup_expired_chaos_experiments,
)
from baldur.celery_tasks.metrics_tasks import (
    check_and_report_sla_breaches,
    collect_baldur_metrics,
)

__all__ = [
    # Circuit Breaker
    "check_circuit_breaker_recovery",
    "expire_manual_overrides",
    "force_close_circuit_breaker",
    "force_open_circuit_breaker",
    # DLQ
    "cleanup_resolved_dlq_entries",
    "conditional_replay_on_circuit_close",
    "replay_batch_by_domain",
    "replay_batch_by_failure_type",
    "replay_single_dlq_entry",
    # Chaos
    "check_recovery_monitoring_experiments",
    # Metrics
    "check_and_report_sla_breaches",
    "collect_baldur_metrics",
    # Drift Detection
    "check_sla_drift",
    "cleanup_expired_chaos_experiments",
    # Audit Buffer Drain
    "flush_redis_audit_buffer",
    "recover_orphaned_processing_queues",
    "apply_audit_buffer_safety_ltrim",
]
