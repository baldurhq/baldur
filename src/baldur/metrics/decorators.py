"""
Metric Tracking Decorators.

Provides decorators for automatic metric tracking.

Universal Async Support:
- Every decorator supports both sync and async functions
- Dispatch is automatic via asyncio.iscoroutinefunction()
- Same structure as the with_jitter pattern
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

import structlog

from baldur.metrics.event_handlers import (
    DLQMetricEventHandler,
    ReplayEventHandler,
)
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_histogram,
)

logger = structlog.get_logger()

P = ParamSpec("P")
R = TypeVar("R")

# Prometheus legacy identifier regexes, kept module-local (not imported from
# prometheus_client.validation) so validity checks run even when prometheus_client
# is absent. Since ~0.21 prometheus_client defaults PROMETHEUS_LEGACY_NAME_VALIDATION
# to False (UTF-8 names) and silently escapes an invalid name at exposition time —
# delivering a different name than advertised with no signal. Validating up front
# makes the outcome deterministic across the whole >=0.17 support range.
_METRIC_NAME_PATTERN = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_KEY_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _is_valid_metric_name(name: str) -> bool:
    """True iff ``name`` matches the Prometheus legacy metric-name regex."""
    return bool(_METRIC_NAME_PATTERN.match(name))


def _is_valid_label_key(key: str) -> bool:
    """True iff ``key`` is a legal label key (reserved ``__`` prefix excluded)."""
    return bool(_LABEL_KEY_PATTERN.match(key)) and not key.startswith("__")


def _create_metric_safe(
    factory: Callable[[str, str, list[str]], Any],
    metric_name: str,
    description: str,
    label_keys: list[str],
) -> Any | None:
    """Create (or fetch) a custom metric at decoration time, fail-open.

    Runs a validity pre-check, then a two-tier fail-open creation, then a
    label-key-mismatch check. Returns the collector, or ``None`` when the metric
    must not be recorded (invalid identifier, prometheus_client absent, or an
    unexpected registry error). Never propagates out of a decorated definition.
    """
    # Validity pre-check (runs even when prometheus_client is absent): an invalid
    # identifier is rejected here rather than silently escaped by the exposition
    # layer (advertised name != delivered name).
    if not _is_valid_metric_name(metric_name):
        logger.warning("metrics.decorator_invalid_name", metric_name=metric_name)
        return None
    invalid_keys = [k for k in label_keys if not _is_valid_label_key(k)]
    if invalid_keys:
        logger.warning(
            "metrics.decorator_invalid_name",
            metric_name=metric_name,
            invalid_label_keys=invalid_keys,
        )
        return None

    # Decoration-time creation, two-tier fail-open (CROSS_SERVICE_STANDARDS split):
    # ImportError is silent (honors the published "recording quietly no-ops when
    # prometheus_client is absent" contract — no per-function warning spam); any
    # other error is defense-in-depth after the pre-check.
    try:
        collector = factory(metric_name, description, label_keys)
    except ImportError:
        return None
    except Exception as e:
        logger.warning(
            "metrics.decorator_registration_failed",
            metric_name=metric_name,
            error=str(e),
        )
        return None

    # Label-key-mismatch detection (once per decorated function): reusing one
    # metric_name with differing label keys returns the first collector, whose
    # per-call .labels() would then raise and be swallowed at DEBUG — invisible
    # when DEBUG is off. Surface the misconfiguration once at decoration time.
    existing = set(getattr(collector, "_labelnames", ()))
    requested = set(label_keys)
    if existing != requested:
        logger.warning(
            "metrics.decorator_label_mismatch",
            metric_name=metric_name,
            requested=sorted(requested),
            existing=sorted(existing),
        )
    return collector


def _record_safe(record_fn: Callable[[], None]) -> None:
    """Run a per-call metric recording, fail-open at DEBUG.

    DEBUG (not the ``_failed``-suffix WARNING default) is deliberate: a persistent
    per-call error would flood logs at request rate, and any persistent
    misconfiguration is already surfaced once at decoration time. Mirrors the
    per-record fail-open of ``MetricsBatchRecorder._flush_loop``.
    """
    try:
        record_fn()
    except Exception as e:
        logger.debug("metrics.decorator_record_failed", error=str(e))


def _record_counter(
    counter: Any | None,
    label_values: dict[str, str],
    *,
    succeeded: bool,
    on_success: bool,
    on_failure: bool,
) -> None:
    """Increment a custom counter for one call outcome, fail-open and flag-aware.

    No-ops when the collector is ``None`` (creation failed/skipped) or when the
    outcome's flag is off (``on_success`` for success, ``on_failure`` for failure).
    """
    if counter is None:
        return
    if not (on_success if succeeded else on_failure):
        return

    def _do() -> None:
        if label_values:
            counter.labels(**label_values).inc()
        else:
            counter.inc()

    _record_safe(_do)


def _record_histogram(
    histogram: Any | None,
    label_values: dict[str, str],
    duration: float,
) -> None:
    """Observe a duration on a custom histogram, fail-open and label-aware.

    No-ops when the collector is ``None`` (creation failed/skipped).
    """
    if histogram is None:
        return

    def _do() -> None:
        if label_values:
            histogram.labels(**label_values).observe(duration)
        else:
            histogram.observe(duration)

    _record_safe(_do)


def track_dlq_creation(domain: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Add metric tracking to a DLQ creation function (sync/async).

    When the decorated function runs successfully, the DLQ creation metric
    is recorded automatically.

    Args:
        domain: Domain name (payment, point, etc.)

    Example:
        >>> @track_dlq_creation(domain="payment")
        ... def create_payment_dlq(failure_type: str, payload: dict):
        ...     return DLQItem.objects.create(...)

        >>> @track_dlq_creation(domain="payment")
        ... async def async_create_dlq(failure_type: str, payload: dict):
        ...     return await DLQItem.objects.acreate(...)
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            result = func(*args, **kwargs)
            failure_type = str(kwargs.get("failure_type", "unknown"))
            DLQMetricEventHandler.on_item_created(domain, failure_type)
            return result

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # ParamSpec/TypeVar pattern doesn't track that R is Awaitable when
            # asyncio.iscoroutinefunction(func) is True — the dispatch is dynamic.
            result = await func(*args, **kwargs)  # type: ignore[misc]
            failure_type = str(kwargs.get("failure_type", "unknown"))
            DLQMetricEventHandler.on_item_created(domain, failure_type)
            return result  # type: ignore[no-any-return]

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper

    return decorator


def track_dlq_resolution(domain: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Add metric tracking to a DLQ resolution function (sync/async).

    When the decorated function runs successfully, the DLQ resolution metric
    is recorded automatically.

    Args:
        domain: Domain name

    Example:
        >>> @track_dlq_resolution(domain="payment")
        ... def resolve_payment_dlq(dlq_item, resolution_type: str = "auto_replay"):
        ...     dlq_item.status = "resolved"
        ...     dlq_item.save()

        >>> @track_dlq_resolution(domain="payment")
        ... async def async_resolve_dlq(dlq_item, resolution_type: str = "auto_replay"):
        ...     dlq_item.status = "resolved"
        ...     await dlq_item.asave()
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start_time = time.monotonic()
            result = func(*args, **kwargs)
            duration = time.monotonic() - start_time
            resolution_type = str(kwargs.get("resolution_type", "auto_replay"))
            DLQMetricEventHandler.on_item_resolved(
                domain=domain,
                resolution_type=resolution_type,
                duration_seconds=duration,
            )
            return result

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start_time = time.monotonic()
            result = await func(*args, **kwargs)  # type: ignore[misc]
            duration = time.monotonic() - start_time
            resolution_type = str(kwargs.get("resolution_type", "auto_replay"))
            DLQMetricEventHandler.on_item_resolved(
                domain=domain,
                resolution_type=resolution_type,
                duration_seconds=duration,
            )
            return result  # type: ignore[no-any-return]

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper

    return decorator


def track_replay(
    domain: str = "",
    replay_type: str = "auto",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Add metric tracking to a replay function (sync/async).

    Tracks replay start/completion automatically and records the duration.

    Args:
        domain: Domain name (extracted from kwargs when empty)
        replay_type: Replay type (auto, manual, batch)

    Example:
        >>> @track_replay(domain="payment")
        ... def sync_replay(dlq_item):
        ...     process_payment(dlq_item.payload)
        ...     return True

        >>> @track_replay(domain="payment")
        ... async def async_replay(dlq_item):
        ...     await process_payment(dlq_item.payload)
        ...     return True
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            _domain = str(domain or kwargs.get("domain", "unknown"))
            _replay_type = str(kwargs.get("replay_type", replay_type))
            ReplayEventHandler.on_replay_started(_domain, _replay_type)

            start_time = time.monotonic()
            success = False
            try:
                result = func(*args, **kwargs)
                success = result if isinstance(result, bool) else True
                return result
            except Exception:
                success = False
                raise
            finally:
                duration = time.monotonic() - start_time
                ReplayEventHandler.on_replay_completed(_domain, success, duration)

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            _domain = str(domain or kwargs.get("domain", "unknown"))
            _replay_type = str(kwargs.get("replay_type", replay_type))
            ReplayEventHandler.on_replay_started(_domain, _replay_type)

            start_time = time.monotonic()
            success = False
            try:
                result = await func(*args, **kwargs)  # type: ignore[misc]
                success = result if isinstance(result, bool) else True
                return result  # type: ignore[no-any-return]
            except Exception:
                success = False
                raise
            finally:
                duration = time.monotonic() - start_time
                ReplayEventHandler.on_replay_completed(_domain, success, duration)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper

    return decorator


def track_execution_time(
    metric_name: str,
    labels: dict[str, str] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Record a function's execution time as a Histogram.

    Args:
        metric_name: Metric name (e.g. "processing_time_seconds")
        labels: Labels to attach

    Example:
        >>> @track_execution_time("payment_processing_seconds", labels={"type": "credit"})
        ... def process_payment(amount: float):
        ...     # Process the payment
        ...     pass
    """
    # Label values are decoration-time constants; normalize to str once so a
    # non-string constant (e.g. labels={"status": 404}) is deterministic rather
    # than a swallowed per-call .labels() error. Keys derive from these values.
    label_values = {k: str(v) for k, v in (labels or {}).items()}
    # Eager creation at decoration time, cached in the closure (prometheus_client
    # default buckets). None means recording is a no-op for this function.
    histogram = _create_metric_safe(
        get_or_create_histogram,
        metric_name,
        "Custom histogram (via @track_execution_time)",
        list(label_values),
    )

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start_time = time.monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                _record_histogram(
                    histogram, label_values, time.monotonic() - start_time
                )

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start_time = time.monotonic()
            try:
                return await func(*args, **kwargs)  # type: ignore[no-any-return,misc]
            finally:
                _record_histogram(
                    histogram, label_values, time.monotonic() - start_time
                )

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper

    return decorator


def track_counter(
    metric_name: str,
    labels: dict[str, str] | None = None,
    on_success: bool = True,
    on_failure: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Record function calls as a Counter.

    Args:
        metric_name: Metric name
        labels: Labels to attach
        on_success: Increment the count on success
        on_failure: Increment the count on failure

    Example:
        >>> @track_counter("api_calls_total", labels={"endpoint": "/payment"})
        ... def payment_api(data: dict):
        ...     return process(data)
    """
    # Label values are decoration-time constants; normalize to str once (see
    # track_execution_time). Keys derive from these values.
    label_values = {k: str(v) for k, v in (labels or {}).items()}
    # Eager creation at decoration time, cached in the closure. None means
    # recording is a no-op for this function.
    counter = _create_metric_safe(
        get_or_create_counter,
        metric_name,
        "Custom counter (via @track_counter)",
        list(label_values),
    )

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                result = func(*args, **kwargs)
            except Exception:
                _record_counter(
                    counter,
                    label_values,
                    succeeded=False,
                    on_success=on_success,
                    on_failure=on_failure,
                )
                raise
            _record_counter(
                counter,
                label_values,
                succeeded=True,
                on_success=on_success,
                on_failure=on_failure,
            )
            return result

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                result = await func(*args, **kwargs)  # type: ignore[misc]
            except Exception:
                _record_counter(
                    counter,
                    label_values,
                    succeeded=False,
                    on_success=on_success,
                    on_failure=on_failure,
                )
                raise
            _record_counter(
                counter,
                label_values,
                succeeded=True,
                on_success=on_success,
                on_failure=on_failure,
            )
            return result  # type: ignore[no-any-return]

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper

    return decorator


__all__ = [
    "track_dlq_creation",
    "track_dlq_resolution",
    "track_replay",
    "track_execution_time",
    "track_counter",
]
