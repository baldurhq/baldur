"""
Celery adapter for the baldur system.

This module provides Celery-specific implementations including:
- Celery tasks for DLQ processing
- Celery tasks for circuit breaker management
- Celery tasks for metric collection
- Celery beat schedule helpers
- **Signal hooks for automatic baldur integration**

Lazy import (PEP 562): heavy submodules — including ``beat_schedule`` which
imports ``kombu`` — are loaded on first attribute access. This keeps
``import baldur.adapters.celery`` working in environments that have baldur
installed without the celery extras (e.g., the testbed django_app), where a
runbook handler may transitively touch this package without ever using
celery beat / kombu internals.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .baldur_task import baldur_task as baldur_task
    from .beat_schedule import (
        BALDUR_QUEUE_CONFIG as BALDUR_QUEUE_CONFIG,
    )
    from .beat_schedule import (
        configure_baldur_celery as configure_baldur_celery,
    )
    from .beat_schedule import (
        get_baldur_beat_schedule as get_baldur_beat_schedule,
    )
    from .beat_schedule import (
        get_baldur_queues as get_baldur_queues,
    )
    from .beat_schedule import (
        get_baldur_task_routes as get_baldur_task_routes,
    )
    from .beat_schedule import (
        get_schedule_summary as get_schedule_summary,
    )
    from .beat_schedule import (
        register_all_tasks_with_celery as register_all_tasks_with_celery,
    )
    from .beat_schedule import (
        validate_schedule as validate_schedule,
    )
    from .signal_config import (
        SignalHooksSettings as SignalHooksSettings,
    )
    from .signal_config import (
        get_signal_hooks_settings as get_signal_hooks_settings,
    )
    from .signal_config import (
        reset_signal_hooks_settings as reset_signal_hooks_settings,
    )
    from .signal_hooks import (
        disconnect_baldur_signals as disconnect_baldur_signals,
    )
    from .signal_hooks import (
        is_signals_connected as is_signals_connected,
    )
    from .signal_hooks import (
        setup_baldur_signals as setup_baldur_signals,
    )
    from .tasks import (
        check_and_report_sla_breaches as check_and_report_sla_breaches,
    )
    from .tasks import (
        check_circuit_breaker_recovery as check_circuit_breaker_recovery,
    )
    from .tasks import (
        cleanup_resolved_dlq_entries as cleanup_resolved_dlq_entries,
    )
    from .tasks import (
        collect_baldur_metrics as collect_baldur_metrics,
    )
    from .tasks import (
        conditional_replay_on_circuit_close as conditional_replay_on_circuit_close,
    )
    from .tasks import (
        expire_manual_overrides as expire_manual_overrides,
    )
    from .tasks import (
        force_close_circuit_breaker as force_close_circuit_breaker,
    )
    from .tasks import (
        force_open_circuit_breaker as force_open_circuit_breaker,
    )
    from .tasks import (
        replay_batch_by_domain as replay_batch_by_domain,
    )
    from .tasks import (
        replay_single_dlq_entry as replay_single_dlq_entry,
    )


_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # baldur_task decorator
    "baldur_task": ("baldur.adapters.celery.baldur_task", "baldur_task"),
    # Beat schedule helpers (transitively imports kombu)
    "BALDUR_QUEUE_CONFIG": (
        "baldur.adapters.celery.beat_schedule",
        "BALDUR_QUEUE_CONFIG",
    ),
    "configure_baldur_celery": (
        "baldur.adapters.celery.beat_schedule",
        "configure_baldur_celery",
    ),
    "get_baldur_beat_schedule": (
        "baldur.adapters.celery.beat_schedule",
        "get_baldur_beat_schedule",
    ),
    "get_baldur_queues": (
        "baldur.adapters.celery.beat_schedule",
        "get_baldur_queues",
    ),
    "get_baldur_task_routes": (
        "baldur.adapters.celery.beat_schedule",
        "get_baldur_task_routes",
    ),
    "get_schedule_summary": (
        "baldur.adapters.celery.beat_schedule",
        "get_schedule_summary",
    ),
    "register_all_tasks_with_celery": (
        "baldur.adapters.celery.beat_schedule",
        "register_all_tasks_with_celery",
    ),
    "validate_schedule": (
        "baldur.adapters.celery.beat_schedule",
        "validate_schedule",
    ),
    # Signal config
    "SignalHooksSettings": (
        "baldur.adapters.celery.signal_config",
        "SignalHooksSettings",
    ),
    "get_signal_hooks_settings": (
        "baldur.adapters.celery.signal_config",
        "get_signal_hooks_settings",
    ),
    "reset_signal_hooks_settings": (
        "baldur.adapters.celery.signal_config",
        "reset_signal_hooks_settings",
    ),
    # Signal hooks
    "disconnect_baldur_signals": (
        "baldur.adapters.celery.signal_hooks",
        "disconnect_baldur_signals",
    ),
    "is_signals_connected": (
        "baldur.adapters.celery.signal_hooks",
        "is_signals_connected",
    ),
    "setup_baldur_signals": (
        "baldur.adapters.celery.signal_hooks",
        "setup_baldur_signals",
    ),
    # Tasks
    "check_and_report_sla_breaches": (
        "baldur.adapters.celery.tasks",
        "check_and_report_sla_breaches",
    ),
    "check_circuit_breaker_recovery": (
        "baldur.adapters.celery.tasks",
        "check_circuit_breaker_recovery",
    ),
    "cleanup_resolved_dlq_entries": (
        "baldur.adapters.celery.tasks",
        "cleanup_resolved_dlq_entries",
    ),
    "collect_baldur_metrics": (
        "baldur.adapters.celery.tasks",
        "collect_baldur_metrics",
    ),
    "conditional_replay_on_circuit_close": (
        "baldur.adapters.celery.tasks",
        "conditional_replay_on_circuit_close",
    ),
    "expire_manual_overrides": (
        "baldur.adapters.celery.tasks",
        "expire_manual_overrides",
    ),
    "force_close_circuit_breaker": (
        "baldur.adapters.celery.tasks",
        "force_close_circuit_breaker",
    ),
    "force_open_circuit_breaker": (
        "baldur.adapters.celery.tasks",
        "force_open_circuit_breaker",
    ),
    "replay_batch_by_domain": (
        "baldur.adapters.celery.tasks",
        "replay_batch_by_domain",
    ),
    "replay_single_dlq_entry": (
        "baldur.adapters.celery.tasks",
        "replay_single_dlq_entry",
    ),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Circuit Breaker Tasks
    "conditional_replay_on_circuit_close",
    "check_circuit_breaker_recovery",
    "force_open_circuit_breaker",
    "force_close_circuit_breaker",
    "expire_manual_overrides",
    # DLQ Tasks
    "replay_single_dlq_entry",
    "replay_batch_by_domain",
    "cleanup_resolved_dlq_entries",
    # Metrics Tasks
    "collect_baldur_metrics",
    "check_and_report_sla_breaches",
    # Signal Hooks
    "setup_baldur_signals",
    "disconnect_baldur_signals",
    "is_signals_connected",
    "get_signal_hooks_settings",
    "reset_signal_hooks_settings",
    "baldur_task",
    "SignalHooksSettings",
    # Beat Schedule
    "get_baldur_beat_schedule",
    "get_baldur_queues",
    "get_baldur_task_routes",
    "configure_baldur_celery",
    "get_schedule_summary",
    "validate_schedule",
    "register_all_tasks_with_celery",
    "BALDUR_QUEUE_CONFIG",
]
