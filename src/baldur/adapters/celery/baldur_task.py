"""
baldur_task decorator — manual baldur integration for individual Celery tasks.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from baldur.adapters.celery.integrations.cb_recorder import (
    CircuitBreakerRecorder,
)
from baldur.adapters.celery.integrations.dlq_recorder import DLQRecorder
from baldur.adapters.celery.signal_config import (
    extract_domain_from_task_name,
    extract_service_name,
    get_signal_hooks_settings,
)

__all__ = ["baldur_task"]


def baldur_task(
    domain: str | None = None,
    service_name: str | None = None,
    track_cb: bool = True,
    track_dlq: bool = True,
) -> Callable:
    """
    Decorator to add baldur tracking to a Celery task.

    Use this for fine-grained control over which tasks are tracked.

    Args:
        domain: Domain for DLQ classification
        service_name: Service name for circuit breaker
        track_cb: Whether to track circuit breaker state
        track_dlq: Whether to store failures in DLQ

    Example:
        @app.task
        @baldur_task(domain='order', service_name='order_service')
        def process_order(order_id):
            # Your task logic
            pass
    """

    def decorator(func: Callable) -> Callable:
        cb_recorder = CircuitBreakerRecorder()
        dlq_recorder = DLQRecorder()

        @wraps(func)
        def wrapper(*args, **kwargs):
            config = get_signal_hooks_settings()
            task_name = func.__name__
            resolved_domain = domain or extract_domain_from_task_name(task_name, config)
            resolved_service = service_name or extract_service_name(task_name, config)

            try:
                result = func(*args, **kwargs)

                # Record success
                if track_cb:
                    cb_recorder.record_success(resolved_service, task_name)

                return result

            except Exception as e:
                # Record failure
                if track_cb:
                    cb_recorder.record_failure(resolved_service, task_name, e)

                if track_dlq:
                    try:
                        from celery import current_task

                        _tid = getattr(
                            getattr(current_task, "request", None), "id", None
                        )
                        task_id = str(_tid) if _tid else ""
                    except Exception:
                        task_id = ""

                    dlq_recorder.store(
                        domain=resolved_domain,
                        task_name=task_name,
                        task_id=task_id,
                        exception=e,
                        args=args,
                        kwargs=kwargs,
                        einfo=None,
                    )

                raise

        return wrapper

    return decorator
