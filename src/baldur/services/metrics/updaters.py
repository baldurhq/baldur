"""
Gauge Update Functions, Context Managers, and Decorators.

Functions for periodic gauge updates from repositories,
context managers for instrumentation, and alerting rule definitions.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import structlog

from baldur.metrics.registry import get_or_create_gauge, get_registered_domains
from baldur.utils.time import utc_now

# Non-domain metric kept locally (was in definitions.py)
_shadow_log_unsynced_count = get_or_create_gauge(
    "baldur_shadow_log_unsynced_count",
    "Number of unsynced shadow log entries",
    [],
)

if TYPE_CHECKING:
    from baldur.interfaces.repositories import (
        CircuitBreakerStateRepository,
        FailedOperationRepository,
    )

logger = structlog.get_logger()


# =============================================================================
# Shadow Log Metrics Update
# =============================================================================


def update_shadow_log_metrics() -> None:
    """Update shadow log metrics from ShadowLogger."""
    try:
        from baldur.adapters.memory.circuit_breaker import get_shadow_logger

        shadow_logger = get_shadow_logger()
        stats = shadow_logger.get_stats()
        _shadow_log_unsynced_count.labels().set(stats.get("unsynced_count", 0))
    except Exception as e:
        logger.warning(
            "metrics.update_shadow_log_failed",
            error=e,
        )


# =============================================================================
# Gauge Update Functions (for periodic collection tasks)
# =============================================================================


def update_dlq_pending_gauges(
    repository: FailedOperationRepository | None = None,
) -> dict[str, int]:
    """
    Update DLQ pending gauges from database.

    Should be called periodically by a scheduled task.

    Args:
        repository: Optional repository instance (uses factory if not provided)

    Returns:
        Dictionary of domain -> pending count
    """
    try:
        if repository is None:
            from baldur.factory import ProviderRegistry

            repository = ProviderRegistry.get_failed_operation_repo()

        stats = repository.get_statistics()
        pending_by_domain = stats.get("pending_by_domain", {})

        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        for domain in get_registered_domains():
            count = pending_by_domain.get(domain, 0)
            metrics.dlq.set_pending_count(domain, count)

        logger.debug(
            "metrics.updated_dlq_pending_gauges",
            pending_by_domain=pending_by_domain,
        )
        return pending_by_domain

    except Exception as e:
        logger.exception(
            "metrics.update_dlq_pending_failed",
            error=e,
        )
        return {}


def update_dlq_status_gauges(
    repository: FailedOperationRepository | None = None,
) -> dict[str, int]:
    """
    Update DLQ status distribution gauges.

    Args:
        repository: Optional repository instance (uses factory if not provided)

    Returns:
        Dictionary of status -> count
    """
    try:
        if repository is None:
            from baldur.factory import ProviderRegistry

            repository = ProviderRegistry.get_failed_operation_repo()

        stats = repository.get_statistics()
        by_status = {
            "pending": stats.get("pending_count", 0),
            "reviewing": stats.get("reviewing_count", 0),
            "resolved": stats.get("resolved_count", 0),
            "rejected": stats.get("rejected_count", 0),
        }

        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        for status, count in by_status.items():
            metrics.dlq.set_status_count(status, count)

        logger.debug(
            "metrics.updated_dlq_status_gauges",
            by_status=by_status,
        )
        return by_status

    except Exception as e:
        logger.exception(
            "metrics.update_dlq_status_failed",
            error=e,
        )
        return {}


def update_circuit_breaker_gauges(
    repository: CircuitBreakerStateRepository | None = None,
) -> dict[str, str]:
    """
    Update circuit breaker state gauges from database.

    Args:
        repository: Optional repository instance (uses factory if not provided)

    Returns:
        Dictionary of service -> state
    """
    try:
        if repository is None:
            from baldur.factory import ProviderRegistry

            repository = ProviderRegistry.get_circuit_breaker_repo()

        from baldur.core.cb_namespace import (
            parse_composite_cb_name,
        )
        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        all_states = repository.get_all_states()
        states = {}
        for cb in all_states:
            base_service, cell_id = parse_composite_cb_name(cb.service_name)
            metrics.circuit_breaker.set_state(base_service, cb.state, cell_id)
            states[cb.service_name] = cb.state

        logger.debug(
            "metrics.updated_circuit_breaker_gauges",
            states=states,
        )
        return states

    except Exception as e:
        logger.exception(
            "metrics.update_circuit_breaker_failed",
            error=e,
        )
        return {}


def update_retry_success_rates(
    repository: FailedOperationRepository | None = None,
) -> dict[str, float]:
    """
    Calculate and update retry success rate gauges.

    Args:
        repository: Optional repository instance (uses factory if not provided)

    Returns:
        Dictionary of domain -> success_rate_percentage
    """
    try:
        if repository is None:
            from baldur.factory import ProviderRegistry

            repository = ProviderRegistry.get_failed_operation_repo()

        stats = repository.get_statistics()
        rates = {}

        success_rates = stats.get("success_rates_by_domain", {})

        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        for domain in get_registered_domains():
            rate = success_rates.get(domain, 100.0)
            metrics.retry.set_success_rate(domain, rate)
            rates[domain] = rate

        logger.debug(
            "metrics.updated_retry_success_rates",
            rates=rates,
        )
        return rates

    except Exception as e:
        logger.exception(
            "metrics.update_retry_success_failed",
            error=e,
        )
        return {}


# =============================================================================
# Context Manager and Decorators for Instrumentation
# =============================================================================


@contextmanager
def track_recovery_time(
    domain: str, resolution_type: str
) -> Generator[None, None, None]:
    """
    Context manager to track recovery time.

    Usage:
        with track_recovery_time("payment", "auto_replay"):
            # perform recovery operation
            pass
    """
    from baldur.utils.time import utc_now

    start = utc_now()
    try:
        yield
    finally:
        from baldur.metrics.prometheus import get_metrics

        end = utc_now()
        get_metrics().retry.record_recovery_time(domain, resolution_type, start, end)


# =============================================================================
# Metric Collection Task Helper
# =============================================================================


def collect_all_metrics() -> dict:
    """
    Collect all baldur metrics.

    This should be called by a periodic Celery task.

    Returns:
        Dictionary with all current metric values
    """
    pending = update_dlq_pending_gauges()
    status = update_dlq_status_gauges()
    cb_states = update_circuit_breaker_gauges()
    success_rates = update_retry_success_rates()

    return {
        "dlq_pending_by_domain": pending,
        "dlq_by_status": status,
        "circuit_breaker_states": cb_states,
        "retry_success_rates": success_rates,
        "collected_at": utc_now().isoformat(),
    }
