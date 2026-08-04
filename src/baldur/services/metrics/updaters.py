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

# Component label of the collection dead-man's switch. The bundled
# BaldurMetricCollectionStale rule selects this exact value, so it is a shipped
# contract rather than a tunable.
METRIC_COLLECTION_HEARTBEAT_COMPONENT = "metric_collection"


# =============================================================================
# Statistics-dict projection helpers
# =============================================================================

# Gauge label -> ``by_status`` source key, for adapters that emit the status
# map instead of the flat ``*_count`` keys (memory, SQL). Written out as an
# explicit projection rather than a name-identity map with one alias, because
# REVIEWING and REQUIRES_REVIEW are BOTH live statuses and both appear in
# ``by_status``: an identity map would land two source keys on the `reviewing`
# gauge label, making its value depend on iteration order. The alias direction
# below mirrors the Redis adapter's own flat keys, where `reviewing_count`
# carries the requires-review count. Every other status is deliberately
# ungauged — same set the flat path publishes.
_STATUS_GAUGE_SOURCE_KEYS = {
    "pending": "pending",
    "reviewing": "requires_review",
    "resolved": "resolved",
    "rejected": "rejected",
}


def _resolve_pending_total(stats: dict) -> int | None:
    """Resolve the repository's pending total from a ``get_statistics()`` dict.

    Adapters disagree on the key shape: the Redis adapter emits a flat
    ``pending_count`` (an O(1) ``ZCARD``), while the memory and SQL adapters
    emit ``by_status`` (an index length / ``GROUP BY`` count). Both are cheap
    and exact, unlike ``pending_by_domain`` — an O(pending) scan that is
    omitted on collection error and under-counts silently when an entry fails
    to decode, so summing it reports 0 against a real backlog.

    Returns:
        The pending total, or ``None`` when the snapshot carries neither shape
        (so callers can tell "no backlog" from "not measured").
    """
    if "pending_count" in stats:
        try:
            return int(stats["pending_count"])
        except (TypeError, ValueError):
            return None

    by_status = stats.get("by_status")
    if isinstance(by_status, dict):
        try:
            return int(by_status.get("pending", 0))
        except (TypeError, ValueError):
            return None

    return None


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
    *,
    stats: dict | None = None,
) -> dict[str, int] | None:
    """
    Update DLQ pending gauges from database.

    Should be called periodically by a scheduled task.

    The per-domain breakdown is a diagnostic surface, not a paging one: it is
    an O(pending) scan that the Redis adapter omits on collection error. When
    it cannot be trusted this function writes **nothing** and returns ``None``,
    so the previously exported values are held. Holding a stale high count
    keeps paging a human; writing zeros would resolve the DLQ alerts during the
    very incident that produced the backlog.

    Args:
        repository: Optional repository instance (uses factory if not provided)
        stats: Optional pre-fetched ``get_statistics()`` snapshot. Callers that
            drive several updaters from one tick pass it so the repository is
            read once instead of once per updater.

    Returns:
        Dictionary of domain -> pending count, or ``None`` when the breakdown
        was unavailable or self-inconsistent (no gauge was written).
    """
    try:
        if stats is None:
            if repository is None:
                from baldur.factory import ProviderRegistry

                repository = ProviderRegistry.get_failed_operation_repo()

            stats = repository.get_statistics()

        if "pending_by_domain" not in stats:
            # Producer failed the breakdown open (the key is dropped, the
            # baseline counts survive) — the WARNING is emitted there.
            logger.debug(
                "metrics.dlq_pending_breakdown_unavailable",
                reason="key_absent",
            )
            return None

        pending_by_domain = stats["pending_by_domain"]
        baseline = _resolve_pending_total(stats) or 0
        if baseline > 0 and not sum(pending_by_domain.values()):
            # Present-but-empty against a live baseline: a mid-call backend
            # degradation flips the breakdown collector onto its empty
            # in-memory fallback and it returns {} without raising. Both
            # numbers come from this one snapshot, so this is a pure local
            # consistency check.
            logger.debug(
                "metrics.dlq_pending_breakdown_unavailable",
                reason="inconsistent_with_baseline",
                pending_count=baseline,
            )
            return None

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
        return None


def update_dlq_status_gauges(
    repository: FailedOperationRepository | None = None,
    *,
    stats: dict | None = None,
) -> dict[str, int] | None:
    """
    Update DLQ status distribution gauges.

    Reads the flat ``*_count`` keys when the adapter emits them (Redis) and
    otherwise projects ``by_status`` (memory / SQL) through
    :data:`_STATUS_GAUGE_SOURCE_KEYS`. The counts here are O(1) on every
    adapter — an index length, a ``GROUP BY`` count or a ``ZCARD`` — which is
    why the ``pending`` row of this family, not the per-domain breakdown, is
    what the bundled DLQ backlog alerts page on.

    Args:
        repository: Optional repository instance (uses factory if not provided)
        stats: Optional pre-fetched ``get_statistics()`` snapshot (see
            :func:`update_dlq_pending_gauges`)

    Returns:
        Dictionary of status -> count, or ``None`` when the snapshot carries
        neither key shape (no gauge was written).
    """
    try:
        if stats is None:
            if repository is None:
                from baldur.factory import ProviderRegistry

                repository = ProviderRegistry.get_failed_operation_repo()

            stats = repository.get_statistics()

        source_by_status = stats.get("by_status")
        if "pending_count" in stats:
            by_status = {
                "pending": stats.get("pending_count", 0),
                "reviewing": stats.get("reviewing_count", 0),
                "resolved": stats.get("resolved_count", 0),
                "rejected": stats.get("rejected_count", 0),
            }
        elif isinstance(source_by_status, dict):
            by_status = {
                gauge_label: source_by_status.get(source_key, 0)
                for gauge_label, source_key in _STATUS_GAUGE_SOURCE_KEYS.items()
            }
        else:
            logger.debug(
                "metrics.dlq_status_source_unavailable",
                reason="no_flat_keys_and_no_by_status",
            )
            return None

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
        return None


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
    *,
    stats: dict | None = None,
) -> dict[str, float] | None:
    """
    Calculate and update retry success rate gauges.

    No repository adapter computes ``success_rates_by_domain`` today, so this
    function normally writes nothing and returns ``None``: the gauge stays
    honestly absent rather than exporting a fabricated 100% for every domain.
    A domain missing from an otherwise-present map is likewise skipped, never
    defaulted.

    Args:
        repository: Optional repository instance (uses factory if not provided)
        stats: Optional pre-fetched ``get_statistics()`` snapshot (see
            :func:`update_dlq_pending_gauges`)

    Returns:
        Dictionary of domain -> success_rate_percentage, or ``None`` when the
        snapshot carries no success-rate source (no gauge was written).
    """
    try:
        if stats is None:
            if repository is None:
                from baldur.factory import ProviderRegistry

                repository = ProviderRegistry.get_failed_operation_repo()

            stats = repository.get_statistics()

        success_rates = stats.get("success_rates_by_domain")
        if not isinstance(success_rates, dict):
            logger.debug(
                "metrics.retry_success_rate_source_unavailable",
                reason="key_absent",
            )
            return None

        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        rates = {}
        for domain in get_registered_domains():
            if domain not in success_rates:
                continue
            rate = success_rates[domain]
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
        return None


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

    Driven per interval by the per-process ``DomainGaugeUpdater``, and
    available to operators who prefer to run collection as a Celery task.

    One ``get_statistics()`` snapshot feeds the three DLQ-family updaters, so
    a tick pays the repository read — the O(pending) breakdown scan on Redis —
    once rather than three times, and the pending updater's cross-key
    consistency check compares two numbers from the same read.

    Emits the ``metric_collection`` heartbeat only when the DLQ **status**
    family was actually written. That family carries the pending total the
    bundled backlog alerts page on, so the dead-man's switch advances exactly
    when the paged gauge is fresh: a Redis incident that costs only the
    per-domain breakdown leaves the paged number current and must not also
    raise a staleness page, while a stalled status write must not leave the
    switch green over a frozen paged series.

    Returns:
        Dictionary with all current metric values
    """
    stats: dict | None = None
    try:
        from baldur.factory import ProviderRegistry

        stats = ProviderRegistry.get_failed_operation_repo().get_statistics()
    except Exception as e:
        logger.exception(
            "metrics.collect_all_snapshot_failed",
            error=e,
        )

    pending = None
    status = None
    success_rates = None
    if stats is not None:
        pending = update_dlq_pending_gauges(stats=stats)
        status = update_dlq_status_gauges(stats=stats)
        success_rates = update_retry_success_rates(stats=stats)

    # Separate repository, separate failure domain — read unconditionally.
    cb_states = update_circuit_breaker_gauges()

    if status is not None:
        from baldur.services.metrics.recorders import emit_heartbeat

        emit_heartbeat(component=METRIC_COLLECTION_HEARTBEAT_COMPONENT)

    return {
        "dlq_pending_by_domain": pending or {},
        "dlq_by_status": status or {},
        "circuit_breaker_states": cb_states,
        "retry_success_rates": success_rates or {},
        "collected_at": utc_now().isoformat(),
    }
