"""Unit tests for the Prometheus query adapter (655 D2).

Covers the family-name guard fix in ``query_error_count`` and its
``_family_name`` helper. ``prometheus_client.collect()`` strips the ``_total``
suffix from a counter's *family* name, so the pre-655 outer guard
(``metric.name == "baldur_dlq_items_total"``) never matched and the method
always returned ``None``. The fix compares against the stripped family name.

Tests inject a fresh ``CollectorRegistry`` via ``prometheus_client.REGISTRY``
patching so they never pollute the global default registry (the same isolation
concern that drives the G49 subprocess snapshot).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import prometheus_client
import pytest
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from baldur.adapters.prometheus_adapter import PrometheusAdapter, _family_name

# A fixed window — query_error_count ignores start/end (in-process registry),
# but a real datetime pair keeps the call signature honest.
_END = datetime(2026, 1, 1, 12, 0, 0)
_START = _END - timedelta(minutes=30)


@pytest.fixture
def isolated_registry():
    """Patch ``prometheus_client.REGISTRY`` with a throwaway registry.

    ``query_error_count`` does ``from prometheus_client import REGISTRY`` at call
    time, so patching the module attribute redirects the lookup to this fresh
    registry without touching the shared global one.
    """
    registry = CollectorRegistry()
    with patch.object(prometheus_client, "REGISTRY", registry):
        yield registry


class TestFamilyNameContract:
    """``_family_name`` strips exactly a trailing ``_total`` (655 D2 helper)."""

    def test_family_name_strips_trailing_total_suffix(self):
        # The whole point of the fix: counter family names drop ``_total``.
        assert _family_name("baldur_dlq_items_total") == "baldur_dlq_items"

    def test_family_name_passes_through_name_without_total_suffix(self):
        # Gauges / histograms have no ``_total`` family suffix → unchanged.
        assert _family_name("baldur_dlq_pending_count") == "baldur_dlq_pending_count"

    def test_family_name_strips_total_only_at_the_end(self):
        # ``_total`` embedded mid-name is not a suffix → left intact.
        assert _family_name("baldur_total_requests") == "baldur_total_requests"

    def test_family_name_empty_string_returns_empty(self):
        assert _family_name("") == ""

    def test_family_name_bare_total_token_strips_to_empty(self):
        # Boundary: the whole name IS the suffix.
        assert _family_name("_total") == ""


class TestQueryErrorCountBehavior:
    """``query_error_count`` matches the stripped family and sums ``_total``."""

    def test_query_error_count_populated_counter_returns_seeded_total(
        self, isolated_registry
    ):
        # Given a populated baldur_dlq_items_total counter (family: baldur_dlq_items)
        counter = Counter(
            "baldur_dlq_items_total",
            "Total DLQ items",
            ["domain"],
            registry=isolated_registry,
        )
        counter.labels(domain="payments").inc(3)
        counter.labels(domain="orders").inc(2)

        # When the adapter queries the default metric name
        adapter = PrometheusAdapter()
        result = adapter.query_error_count(_START, _END)

        # Then it returns the summed _total value (the pre-655 bug returned None)
        assert result == 5

    def test_query_error_count_empty_registry_returns_none(self, isolated_registry):
        # No matching family registered at all → None (sentinel for "unavailable").
        adapter = PrometheusAdapter()
        assert adapter.query_error_count(_START, _END) is None

    def test_query_error_count_excludes_created_timestamp_samples(
        self, isolated_registry
    ):
        # A counter family also emits a `_created` timestamp sample; only `_total`
        # samples must be summed, otherwise the count balloons by a unix epoch.
        counter = Counter(
            "baldur_dlq_items_total",
            "Total DLQ items",
            ["domain"],
            registry=isolated_registry,
        )
        counter.labels(domain="payments").inc(7)

        adapter = PrometheusAdapter()

        # 7, not ~1.7e9 — proves the `_created` sample was filtered out.
        assert adapter.query_error_count(_START, _END) == 7

    def test_query_error_count_label_filter_selects_matching_sample(
        self, isolated_registry
    ):
        # Given two label sets on the same family
        counter = Counter(
            "baldur_dlq_items_total",
            "Total DLQ items",
            ["domain"],
            registry=isolated_registry,
        )
        counter.labels(domain="payments").inc(3)
        counter.labels(domain="orders").inc(2)

        adapter = PrometheusAdapter()

        # When filtered to one label, only that sample contributes
        assert (
            adapter.query_error_count(_START, _END, labels={"domain": "payments"}) == 3
        )
        assert adapter.query_error_count(_START, _END, labels={"domain": "orders"}) == 2

    def test_query_error_count_label_filter_no_match_returns_zero(
        self, isolated_registry
    ):
        # Family present but no sample matches the filter → 0 (family matched, sum 0),
        # distinct from None (family absent).
        counter = Counter(
            "baldur_dlq_items_total",
            "Total DLQ items",
            ["domain"],
            registry=isolated_registry,
        )
        counter.labels(domain="payments").inc(3)

        adapter = PrometheusAdapter()
        assert adapter.query_error_count(_START, _END, labels={"domain": "ghost"}) == 0

    def test_query_error_count_custom_metric_name_without_total_suffix(
        self, isolated_registry
    ):
        # A gauge-shaped name (no `_total`) must still match via passthrough family.
        gauge = Gauge(
            "baldur_custom_errors",
            "Custom error gauge",
            registry=isolated_registry,
        )
        gauge.set(9)

        adapter = PrometheusAdapter()
        result = adapter.query_error_count(
            _START, _END, metric_name="baldur_custom_errors"
        )

        # The inner check matches `sample.name == metric_name` for the gauge sample.
        assert result == 9

    def test_query_error_count_missing_family_returns_none(self, isolated_registry):
        # A populated-but-unrelated family does not satisfy the query.
        Counter(
            "baldur_dlq_items_total",
            "Total DLQ items",
            ["domain"],
            registry=isolated_registry,
        ).labels(domain="payments").inc(1)

        adapter = PrometheusAdapter()
        assert (
            adapter.query_error_count(_START, _END, metric_name="baldur_absent_total")
            is None
        )

    def test_query_error_count_total_named_gauge_matches_via_raw_name(
        self, isolated_registry
    ):
        # A gauge whose own NAME ends in `_total`: prometheus does NOT strip the
        # suffix from a non-counter family (only counters get stripped), so
        # `_family_name` over-strips it to `baldur_active`. The outer guard must
        # also compare the raw `metric_name`, else this populated series is
        # silently unmatched (the bug query introduced by the family-strip).
        gauge = Gauge(
            "baldur_active_total",
            "Active total gauge",
            registry=isolated_registry,
        )
        gauge.set(4)

        adapter = PrometheusAdapter()

        # The single gauge sample `baldur_active_total` ends with `_total` → summed.
        assert (
            adapter.query_error_count(_START, _END, metric_name="baldur_active_total")
            == 4
        )


class TestQueryMetricBehavior:
    """``query_metric`` matches both the stripped family and the raw name."""

    def test_query_metric_total_named_histogram_matches_via_raw_name(
        self, isolated_registry
    ):
        # The live `retry_attempts_total` is a Histogram; prometheus keeps the
        # full `_total` family name for histograms. `_family_name` over-strips it
        # to `retry_attempts`, so without the raw-name fallback the guard never
        # matches and query_metric returns None for a populated series.
        histogram = Histogram(
            "retry_attempts_total",
            "Retry attempts",
            registry=isolated_registry,
        )
        histogram.observe(3)

        adapter = PrometheusAdapter()
        result = adapter.query_metric("retry_attempts_total")

        # Family matched (a sample value is returned) — pre-fix this was None.
        assert result is not None

    def test_query_metric_absent_family_returns_none(self, isolated_registry):
        adapter = PrometheusAdapter()
        assert adapter.query_metric("baldur_absent_metric") is None
