"""
Metric Reconciler for Lazy Synchronization.

Provides mechanisms to synchronize Gauge metrics with actual data sources
during server startup and on-demand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from baldur.adapters.metrics.base import (
    MetricSourceAdapter,
)
from baldur.adapters.metrics.factory import get_metric_adapter
from baldur.metrics.safe_gauge import clamp_non_negative, clamp_percentage
from baldur.utils.jitter import with_jitter
from baldur.utils.time import utc_now

logger = structlog.get_logger()


@dataclass
class SyncResult:
    """Synchronization result."""

    synced_at: str = field(default_factory=lambda: utc_now().isoformat())
    dlq_pending: dict[str, int] = field(default_factory=dict)
    circuit_breaker_states: dict[str, str] = field(default_factory=dict)
    retry_success_rates: dict[str, float] = field(default_factory=dict)


class MetricReconciler:
    """
    Reconciler that aligns metrics with the actual data sources.

    Synchronizes only Gauge-type metrics.
    Counter and Histogram are accurate at Push time, so synchronization is unnecessary.

    Note:
        All time handling uses timezone-aware datetime.
        datetime.utcnow() is deprecated in Python 3.12, so
        baldur.utils.time.utc_now() is used.

    Example:
        >>> adapter = get_metric_adapter()
        >>> reconciler = MetricReconciler(adapter)
        >>> result = reconciler.sync_all_gauges()
        >>> print(f"Synced at: {result.synced_at}")
    """

    def __init__(
        self,
        adapter: MetricSourceAdapter | None = None,
        domains: list[str] | None = None,
        services: list[str] | None = None,
    ):
        """
        Initialize MetricReconciler.

        Args:
            adapter: metric source adapter (obtained from the factory if None)
            domains: domain list (uses registered domains if None)
            services: service list (for Circuit Breaker; loaded from settings if None)
        """
        self.adapter = adapter or get_metric_adapter()
        self._domains = domains
        self._services = services  # Domain-free: load from settings or pass explicitly
        self._last_sync: datetime | None = None
        self._last_sync_result: SyncResult | None = None

    def _get_domains(self) -> list[str]:
        """Return the domain list."""
        if self._domains:
            return self._domains
        try:
            from baldur.metrics.registry import get_registered_domains

            return get_registered_domains()
        except ImportError:
            return ["external_service", "internal_process", "async_task"]

    def _get_services(self) -> list[str]:
        """
        Return the service list (for Circuit Breaker).

        Domain-Free design:
        - Use explicitly passed services if present
        - Otherwise try loading from settings
        - If settings are also absent, return an empty list (dynamic discovery possible)
        """
        if self._services:
            return self._services
        try:
            from baldur.settings.circuit_breaker import (
                get_circuit_breaker_settings,
            )

            return get_circuit_breaker_settings().monitored_services
        except Exception:
            return []

    def _get_metrics(self):
        """Return the metrics instance."""
        try:
            from baldur.metrics.prometheus import get_metrics

            return get_metrics()
        except ImportError:
            return None

    def sync_all_gauges(self) -> SyncResult:
        """
        Synchronize all Gauge metrics with the data sources.

        Returns:
            Synchronization result (SyncResult)
        """
        result = SyncResult()
        metrics = self._get_metrics()

        # Synchronize DLQ pending counts
        for domain in self._get_domains():
            try:
                actual = self.adapter.get_dlq_pending_count(domain)
                # Negative guard: use the clamp_non_negative utility
                safe_actual = clamp_non_negative(actual, f"dlq_pending[{domain}]")
                result.dlq_pending[domain] = int(safe_actual)

                recorder = getattr(metrics, "dlq", None)
                if recorder is not None:
                    recorder.set_pending_count(domain, int(safe_actual))
            except Exception as e:
                logger.warning(
                    "reconciler.sync_dlq_pending_failed",
                    healing_domain=domain,
                    error=e,
                )

        # Synchronize Circuit Breaker states
        for service in self._get_services():
            try:
                state = self.adapter.get_circuit_breaker_state(service)
                result.circuit_breaker_states[service] = state

                recorder = getattr(metrics, "circuit_breaker", None)
                if recorder is not None:
                    from baldur.core.cb_namespace import (
                        parse_composite_cb_name,
                    )

                    base_service, cell_id = parse_composite_cb_name(service)
                    # The recorder maps the state string -> int internally.
                    recorder.set_state(base_service, state, cell_id=cell_id)
            except Exception as e:
                logger.warning(
                    "reconciler.sync_cb_state_failed",
                    target_service=service,
                    error=e,
                )

        # Synchronize retry success rates
        for domain in self._get_domains():
            try:
                rate = self.adapter.get_retry_success_rate(domain)
                # Clamp to the 0-100 range: use the clamp_percentage utility
                safe_rate = clamp_percentage(rate, f"retry_success_rate[{domain}]")
                result.retry_success_rates[domain] = safe_rate

                recorder = getattr(metrics, "retry", None)
                if recorder is not None:
                    recorder.set_success_rate(domain, safe_rate)
            except Exception as e:
                logger.warning(
                    "reconciler.sync_retry_rate_failed",
                    healing_domain=domain,
                    error=e,
                )

        self._last_sync = utc_now()
        self._last_sync_result = result
        logger.info(
            "reconciler.metrics_reconciled",
            dlq_pending_count=len(result.dlq_pending),
        )

        return result

    @with_jitter(max_delay_seconds=60.0)
    def sync_all_gauges_with_jitter(self) -> SyncResult:
        """
        Gauge synchronization with jitter applied.

        Use this method at server startup to
        spread DB load in a distributed environment.

        Returns:
            Synchronization result
        """
        return self.sync_all_gauges()

    def sync_domain_gauges(self, domain: str) -> dict[str, Any]:
        """
        Synchronize only the Gauges of a specific domain.

        Args:
            domain: domain name

        Returns:
            Synchronization result dictionary
        """
        metrics = self._get_metrics()

        actual = self.adapter.get_dlq_pending_count(domain)
        # Negative guard: use the clamp_non_negative utility
        safe_actual = clamp_non_negative(actual, f"dlq_pending[{domain}]")
        dlq_recorder = getattr(metrics, "dlq", None)
        if dlq_recorder is not None:
            dlq_recorder.set_pending_count(domain, int(safe_actual))

        rate = self.adapter.get_retry_success_rate(domain)
        # Clamp to the 0-100 range: use the clamp_percentage utility
        safe_rate = clamp_percentage(rate, f"retry_success_rate[{domain}]")
        retry_recorder = getattr(metrics, "retry", None)
        if retry_recorder is not None:
            retry_recorder.set_success_rate(domain, safe_rate)

        return {"domain": domain, "dlq_pending": safe_actual, "retry_rate": safe_rate}

    @property
    def last_sync_time(self) -> datetime | None:
        """Last synchronization time."""
        return self._last_sync


# Singleton instance
import threading

_reconciler_instance: MetricReconciler | None = None
_reconciler_lock = threading.Lock()


def get_reconciler(
    adapter: MetricSourceAdapter | None = None,
) -> MetricReconciler:
    """
    Return the MetricReconciler singleton instance.

    Args:
        adapter: the adapter to use (uses the default adapter if None)

    Returns:
        A MetricReconciler instance
    """
    global _reconciler_instance

    if _reconciler_instance is None:
        with _reconciler_lock:
            if _reconciler_instance is None:
                _reconciler_instance = MetricReconciler(adapter=adapter)

    return _reconciler_instance


def reset_reconciler() -> None:
    """Reset the Reconciler instance (for tests)."""
    global _reconciler_instance
    with _reconciler_lock:
        _reconciler_instance = None


__all__ = [
    "SyncResult",
    "MetricReconciler",
    "get_reconciler",
    "reset_reconciler",
]
