"""
Prometheus Query Adapter.

Provides a thin adapter for querying Prometheus metrics,
with graceful degradation when prometheus_client or a Prometheus
server is unavailable.

Usage:
    from baldur.adapters.prometheus_adapter import get_prometheus_adapter

    adapter = get_prometheus_adapter()
    if adapter:
        count = adapter.query_error_count(start=start_dt, end=end_dt)
"""

from __future__ import annotations

from datetime import datetime

import structlog

logger = structlog.get_logger()

# Check if prometheus_client is available
try:
    import prometheus_client  # noqa: F401

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


class PrometheusAdapter:
    """
    Adapter for querying Prometheus metrics.

    Provides error count queries and other metric lookups used
    by intelligence tasks and reconciliation services.

    This adapter queries the local prometheus_client registry
    (in-process metrics). For remote Prometheus server queries,
    extend with HTTP client support.
    """

    def __init__(self) -> None:
        if not PROMETHEUS_AVAILABLE:
            raise RuntimeError("prometheus_client is not installed")

    def query_error_count(
        self,
        start: datetime,
        end: datetime,
        metric_name: str = "baldur_dlq_items_total",
        labels: dict[str, str] | None = None,
    ) -> int | None:
        """
        Query the total error count for a given time range.

        Queries the in-process prometheus_client registry for the
        specified counter metric. Returns the current counter value
        as an approximation (counters are monotonically increasing).

        Args:
            start: Start of the query window (currently unused for
                   in-process registry; reserved for remote queries).
            end: End of the query window (currently unused for
                 in-process registry; reserved for remote queries).
            metric_name: Prometheus metric name to query.
            labels: Optional label filters.

        Returns:
            Error count as integer, or None if metric is unavailable.
        """
        try:
            from prometheus_client import REGISTRY

            family_name = _family_name(metric_name)
            for metric in REGISTRY.collect():
                if metric.name == family_name or metric.name == metric_name:
                    total = 0.0
                    for sample in metric.samples:
                        if sample.name.endswith("_total") or sample.name == metric_name:
                            if labels and not _labels_match(sample.labels, labels):
                                continue
                            total += sample.value
                    return int(total)

            return None

        except Exception as e:
            logger.debug(
                "prometheus_adapter.query_error_count_failed",
                metric_name=metric_name,
                error=str(e),
            )
            return None

    def query_metric(
        self,
        metric_name: str,
        labels: dict[str, str] | None = None,
    ) -> float | None:
        """
        Query a single metric value from the in-process registry.

        Args:
            metric_name: Prometheus metric name.
            labels: Optional label filters.

        Returns:
            Metric value as float, or None if unavailable.
        """
        try:
            from prometheus_client import REGISTRY

            family_name = _family_name(metric_name)
            for metric in REGISTRY.collect():
                if metric.name == family_name or metric.name == metric_name:
                    for sample in metric.samples:
                        if labels and not _labels_match(sample.labels, labels):
                            continue
                        return sample.value

            return None

        except Exception as e:
            logger.debug(
                "prometheus_adapter.query_metric_failed",
                metric_name=metric_name,
                error=str(e),
            )
            return None


def _family_name(metric_name: str) -> str:
    """Strip a trailing ``_total`` to recover a counter's Prometheus family name.

    ``prometheus_client.collect()`` strips the ``_total`` suffix from a
    **counter's** family name (the family for ``baldur_dlq_items_total`` is
    ``baldur_dlq_items``; the per-sample names keep the suffix). Comparing the
    raw ``metric_name`` against ``metric.name`` therefore never matches a
    counter, so the collect loop silently returns nothing — this helper recovers
    the family name so the guard matches.

    Gauges and histograms, however, keep their full family name even when it ends
    in ``_total`` (e.g. the live ``retry_attempts_total`` histogram). For those
    this helper over-strips, so callers compare ``metric.name`` against BOTH the
    stripped family name AND the raw ``metric_name``; the latter matches a
    gauge/histogram whose own name ends in ``_total``.
    """
    if metric_name.endswith("_total"):
        return metric_name[: -len("_total")]
    return metric_name


def _labels_match(
    sample_labels: dict[str, str],
    required_labels: dict[str, str],
) -> bool:
    """Check if sample labels match all required label filters."""
    return all(sample_labels.get(k) == v for k, v in required_labels.items())


# =============================================================================
# Singleton Pattern
# =============================================================================


def _create_prometheus_adapter() -> PrometheusAdapter | None:
    if not PROMETHEUS_AVAILABLE:
        logger.debug("prometheus_adapter.prometheus_client_unavailable")
        return None
    try:
        return PrometheusAdapter()
    except Exception as e:
        logger.debug("prometheus_adapter.init_failed", error=str(e))
        return None


from baldur.utils.singleton import make_singleton_factory

get_prometheus_adapter, configure_prometheus_adapter, reset_prometheus_adapter = (
    make_singleton_factory("prometheus_adapter", _create_prometheus_adapter)
)


__all__ = [
    "PrometheusAdapter",
    "get_prometheus_adapter",
    "configure_prometheus_adapter",
    "reset_prometheus_adapter",
]
