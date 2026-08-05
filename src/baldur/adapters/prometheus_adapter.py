"""
Prometheus Query Adapter.

Provides a thin adapter for querying Prometheus metrics,
with graceful degradation when prometheus_client or a Prometheus
server is unavailable.

Reads the *in-process* ``prometheus_client`` registry (no network). For
*remote* PromQL over HTTP against a Prometheus server or PromQL-compatible
backend, use ``baldur.adapters.metrics.prometheus_query_client`` instead.

Usage:
    from baldur.adapters.prometheus_adapter import get_prometheus_adapter

    adapter = get_prometheus_adapter()
    if adapter:
        count = adapter.query_error_count(start=start_dt, end=end_dt)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from prometheus_client.samples import Sample

logger = structlog.get_logger()

# Quantile the console's latency tokens report. Named rather than inlined so the
# reducer and its callers cannot drift apart.
P95_QUANTILE = 0.95

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

    def collect_families(
        self,
        names: Sequence[str],
    ) -> dict[str, list[Sample]]:
        """Collect several metric families in a **single** registry walk.

        A registry walk costs O(total registered label sets), so a caller
        needing several families must not walk once per family. This walks once
        and returns the samples of every requested family.

        Matching is **exact-name**, never a prefix: ``metric.name`` is compared
        against both the ``_total``-stripped counter family name and the raw
        requested name (the same dual compare the single-metric queries use).
        A prefix match would, for example, feed the counter
        ``baldur_dlq_replay_dispatch_total`` into a caller asking for the
        histogram ``baldur_dlq_replay_duration_seconds``.

        Args:
            names: Metric names to collect.

        Returns:
            ``{requested_name: samples}``. A family that is not registered is
            **absent from the mapping** — distinguishable from a registered
            family that has no samples (present, empty list).
        """
        collected: dict[str, list[Sample]] = {}
        try:
            from prometheus_client import REGISTRY

            for metric in REGISTRY.collect():
                for name in names:
                    if metric.name == _family_name(name) or metric.name == name:
                        collected[name] = list(metric.samples)
        except Exception as e:
            logger.debug(
                "prometheus_adapter.collect_families_failed",
                metric_names=list(names),
                error=str(e),
            )
            return {}

        return collected

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
    in ``_total`` — a histogram registered as ``x_total`` collects as ``x_total``,
    never as the counter-style stripped ``x``. For those
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


def sum_counter(
    samples: Sequence[Sample],
    labels: dict[str, str] | None = None,
) -> int:
    """Sum a **counter** family's samples, optionally filtered by labels.

    Pure over an already-collected sample list — performs no registry access.

    Only ``_total`` samples are summed, so a counter's ``_created`` companion
    never inflates the result. That same filter makes this function unusable on
    a histogram family: histogram samples are named ``_bucket`` / ``_count`` /
    ``_sum``, none of which end in ``_total``, so the sum would be a silent
    ``0``. Histogram families go through :func:`p95_from_buckets` only.

    Args:
        samples: Samples of one counter family.
        labels: Optional label filter — a sample matches when it carries every
            given key with the given value.

    Returns:
        The summed value, truncated to an integer.
    """
    total = 0.0
    for sample in samples:
        if not sample.name.endswith("_total"):
            continue
        if labels and not _labels_match(sample.labels, labels):
            continue
        total += sample.value
    return int(total)


def p95_from_buckets(samples: Sequence[Sample]) -> float | None:
    """Interpolate the p95 of a **histogram** family from its bucket samples.

    Pure over an already-collected sample list — performs no registry access.

    Bucket counts are merged across every label set of the family, which is
    sound because a family's bucket edges are identical for all label sets
    (``prometheus_client`` takes the edges once, at family creation). The
    quantile is then interpolated inside the containing bucket, the same way
    PromQL's ``histogram_quantile`` does.

    Args:
        samples: Samples of one histogram family.

    Returns:
        The interpolated p95 in the family's own unit, or ``None`` when the
        merged observation count is zero — a caller must render nothing rather
        than a fabricated ``0``.
    """
    merged: dict[str, float] = {}
    for sample in samples:
        if not sample.name.endswith("_bucket"):
            continue
        bound = sample.labels.get("le")
        if bound is None:
            continue
        merged[bound] = merged.get(bound, 0.0) + sample.value

    if not merged:
        return None

    bounds = sorted(merged, key=_bucket_bound)
    cumulative = [merged[bound] for bound in bounds]
    observations = cumulative[-1]
    if observations <= 0:
        return None

    rank = P95_QUANTILE * observations
    index = 0
    while index < len(cumulative) - 1 and cumulative[index] < rank:
        index += 1

    upper = _bucket_bound(bounds[index])
    if upper == float("inf"):
        # The quantile lands in the overflow bucket: report the highest finite
        # bound rather than infinity (the Prometheus convention).
        finite = [_bucket_bound(b) for b in bounds if _bucket_bound(b) != float("inf")]
        return finite[-1] if finite else None

    lower = _bucket_bound(bounds[index - 1]) if index > 0 else 0.0
    below = cumulative[index - 1] if index > 0 else 0.0
    in_bucket = cumulative[index] - below
    if in_bucket <= 0:
        return upper
    return lower + (upper - lower) * ((rank - below) / in_bucket)


def _bucket_bound(le: str) -> float:
    """Parse a histogram bucket's ``le`` label into a comparable float."""
    try:
        return float(le)
    except (TypeError, ValueError):
        return float("inf")


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
    "P95_QUANTILE",
    "PrometheusAdapter",
    "configure_prometheus_adapter",
    "get_prometheus_adapter",
    "p95_from_buckets",
    "reset_prometheus_adapter",
    "sum_counter",
]
