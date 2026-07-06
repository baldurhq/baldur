"""Dummy Celery tasks for baldur integration tests.

Replaces shopping.tasks so CB, DLQ, and retry tests can run
without host-app dependencies.

Design decisions (§7.4):
- always_failing_task: immediate RuntimeError for CB threshold / DLQ tests
- deterministic_failing_task: failure_rate param (1.0=always fail, 0.0=always pass)
  for CB half-open testing — NO random usage, fully reproducible
- slow_task: time.sleep(delay) for worker timeout / daemon thread tests
"""

import time

from celery import shared_task

DEFAULT_SLOW_TASK_DELAY = 5.0
SLOW_TASK_SOFT_TIME_LIMIT = 10


@shared_task
def dummy_payment_task(order_id: int):
    """Always-succeeding payment task."""
    return {"order_id": order_id, "status": "processed"}


@shared_task
def dummy_order_task(order_id: int):
    """Always-succeeding order task."""
    return {"order_id": order_id, "status": "completed"}


@shared_task
def always_failing_task(order_id: int):
    """Immediately raises RuntimeError.

    Use for: CB failure_threshold, DLQ storage verification.
    """
    raise RuntimeError(f"Deterministic failure for order {order_id}")


@shared_task
def deterministic_failing_task(order_id: int, *, failure_rate: float = 1.0):
    """Fails based on invocation count vs failure_rate threshold.

    Unlike random-based approaches, this uses a counter so tests
    are fully deterministic and reproducible.

    Args:
        order_id: Target order identifier.
        failure_rate: Float 0.0-1.0. Use 1.0 (always fail) or 0.0 (always succeed)
            for deterministic tests. Intermediate values use modular arithmetic
            on the call counter for reproducibility.
    """
    if failure_rate >= 1.0:
        raise RuntimeError(f"Deterministic failure for order {order_id}")
    if failure_rate <= 0.0:
        return {"order_id": order_id, "status": "processed"}

    counter = getattr(deterministic_failing_task, "_call_counter", 0) + 1
    deterministic_failing_task._call_counter = counter

    cycle_length = round(1.0 / failure_rate) if failure_rate > 0 else 0
    if cycle_length > 0 and counter % cycle_length == 0:
        raise RuntimeError(f"Deterministic failure #{counter} for order {order_id}")

    return {"order_id": order_id, "status": "processed", "call_number": counter}


@shared_task(soft_time_limit=SLOW_TASK_SOFT_TIME_LIMIT)
def slow_task(order_id: int, *, delay: float = DEFAULT_SLOW_TASK_DELAY):
    """Sleeps for `delay` seconds before returning.

    Use for: worker timeout, daemon thread interaction tests.
    soft_time_limit=10 ensures Celery raises SoftTimeLimitExceeded
    when delay exceeds the limit.
    """
    time.sleep(delay)
    return {"order_id": order_id, "status": "slow_completed", "delay": delay}
