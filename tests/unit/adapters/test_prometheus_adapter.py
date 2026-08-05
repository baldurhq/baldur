"""Unit tests for the Prometheus query adapter (655 D2).

Covers the family-name guard fix in ``query_error_count`` and its
``_family_name`` helper. ``prometheus_client.collect()`` strips the ``_total``
suffix from a counter's *family* name, so the pre-655 outer guard
(``metric.name == "baldur_dlq_items_total"``) never matched and the method
always returned ``None``. The fix compares against the stripped family name.

Also covers the multi-family read the admin healing summary is built on:
``collect_families`` (one registry walk for several families, exact-name
matching, absent distinguishable from empty) and the two pure reducers over its
output, ``sum_counter`` and ``p95_from_buckets``.

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
from prometheus_client.samples import Sample

from baldur.adapters.prometheus_adapter import (
    P95_QUANTILE,
    PrometheusAdapter,
    _family_name,
    p95_from_buckets,
    sum_counter,
)

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
        # A Histogram named `..._total`: prometheus keeps the full `_total`
        # family name for histograms. `_family_name` over-strips it to
        # `retry_attempts`, so without the raw-name fallback the guard never
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


# =============================================================================
# Multi-family read + pure reducers
# =============================================================================

_REPLAY_DURATION = "baldur_dlq_replay_duration_seconds"
_REPLAY_DISPATCH = "baldur_dlq_replay_dispatch_total"


class _CountingRegistry:
    """Registry wrapper that records how many times it was walked.

    ``collect_families``' whole reason to exist is that a walk costs
    O(total registered label sets), so the number of walks is the property
    under test — not something a return-value assertion can see.
    """

    def __init__(self, inner: CollectorRegistry) -> None:
        self._inner = inner
        self.walks = 0

    def collect(self):
        self.walks += 1
        return self._inner.collect()


class _RaisingRegistry:
    """Registry whose walk fails, to exercise the fail-open arm."""

    def __init__(self) -> None:
        self.walked = False

    def collect(self):
        self.walked = True
        raise RuntimeError("registry corrupted")


def _bucket_samples(
    family: str, cumulative: dict[str, float], **labels: str
) -> list[Sample]:
    """Hand-build one label set's cumulative ``_bucket`` samples."""
    return [
        Sample(f"{family}_bucket", {**labels, "le": le}, count)
        for le, count in cumulative.items()
    ]


class TestCollectFamiliesBehavior:
    """One walk, exact-name matching, absent distinguishable from empty."""

    def test_collect_families_walks_the_registry_once_for_many_names(
        self, isolated_registry
    ):
        # Given three populated families
        Counter(
            "baldur_replay_outcomes_total",
            "Replay outcomes",
            ["outcome"],
            registry=isolated_registry,
        ).labels(outcome="success").inc(4)
        Counter(
            "baldur_watchdog_escalation_total",
            "Escalations",
            ["result"],
            registry=isolated_registry,
        ).labels(result="sent").inc(1)
        Histogram(
            _REPLAY_DURATION,
            "Replay duration",
            registry=isolated_registry,
        ).observe(0.5)
        counting = _CountingRegistry(isolated_registry)

        # When all three are requested together
        with patch.object(prometheus_client, "REGISTRY", counting):
            collected = PrometheusAdapter().collect_families(
                [
                    "baldur_replay_outcomes_total",
                    "baldur_watchdog_escalation_total",
                    _REPLAY_DURATION,
                ]
            )

        # Then one walk answered all three — not one walk per family
        assert counting.walks == 1
        assert set(collected) == {
            "baldur_replay_outcomes_total",
            "baldur_watchdog_escalation_total",
            _REPLAY_DURATION,
        }

    def test_collect_families_unregistered_family_is_absent_from_the_mapping(
        self, isolated_registry
    ):
        # A name nothing registered must not appear as an empty list — the
        # caller has to tell "no such family" from "family, no samples".
        collected = PrometheusAdapter().collect_families(["baldur_absent_total"])

        assert "baldur_absent_total" not in collected
        assert collected == {}

    def test_collect_families_registered_family_without_children_is_empty_list(
        self, isolated_registry
    ):
        # A labelled counter with no child yet: the family IS registered, and
        # it collects zero samples. Present-with-empty, not absent.
        Counter(
            "baldur_replay_outcomes_total",
            "Replay outcomes",
            ["outcome"],
            registry=isolated_registry,
        )

        collected = PrometheusAdapter().collect_families(
            ["baldur_replay_outcomes_total"]
        )

        assert "baldur_replay_outcomes_total" in collected
        assert collected["baldur_replay_outcomes_total"] == []

    def test_collect_families_matches_the_stripped_counter_family_name(
        self, isolated_registry
    ):
        # prometheus strips `_total` from a counter's FAMILY name, so the
        # requested wire name only matches through the dual compare.
        Counter(
            "baldur_replay_outcomes_total",
            "Replay outcomes",
            ["outcome"],
            registry=isolated_registry,
        ).labels(outcome="success").inc(2)

        collected = PrometheusAdapter().collect_families(
            ["baldur_replay_outcomes_total"]
        )

        assert sum_counter(collected["baldur_replay_outcomes_total"]) == 2

    def test_collect_families_shared_prefix_counter_does_not_answer_a_histogram(
        self, isolated_registry
    ):
        # Given only the counter that shares the histogram's prefix
        Counter(
            _REPLAY_DISPATCH,
            "Replay dispatches",
            ["domain"],
            registry=isolated_registry,
        ).labels(domain="payments").inc(3)

        # When the histogram family is requested
        collected = PrometheusAdapter().collect_families([_REPLAY_DURATION])

        # Then it is absent. A prefix match would hand counter samples to
        # p95_from_buckets and fabricate a quantile from a bucketless family.
        assert _REPLAY_DURATION not in collected

    def test_collect_families_returns_empty_mapping_when_the_walk_raises(self):
        # Fail-open: the healing payload omits every field rather than 500ing.
        raising = _RaisingRegistry()

        with patch.object(prometheus_client, "REGISTRY", raising):
            collected = PrometheusAdapter().collect_families(["baldur_anything_total"])

        assert raising.walked, "the fault must actually fire, not be skipped"
        assert collected == {}


class TestSumCounterContract:
    """Sums ``_total`` samples only, under an optional label filter."""

    def test_sum_counter_empty_sample_list_returns_zero(self):
        assert sum_counter([]) == 0

    def test_sum_counter_adds_every_total_sample_across_label_sets(self):
        samples = [
            Sample("baldur_replay_outcomes_total", {"outcome": "success"}, 4.0),
            Sample("baldur_replay_outcomes_total", {"outcome": "failure"}, 3.0),
        ]

        assert sum_counter(samples) == 7

    def test_sum_counter_label_filter_selects_the_matching_subset(self):
        samples = [
            Sample(
                "baldur_replay_outcomes_total",
                {"outcome": "success", "is_synthetic": "false"},
                4.0,
            ),
            Sample(
                "baldur_replay_outcomes_total",
                {"outcome": "success", "is_synthetic": "true"},
                9.0,
            ),
            Sample(
                "baldur_replay_outcomes_total",
                {"outcome": "failure", "is_synthetic": "false"},
                3.0,
            ),
        ]

        matched = sum_counter(samples, {"outcome": "success", "is_synthetic": "false"})

        # Only the one sample carrying BOTH label values — synthetic traffic and
        # failures are somebody else's number.
        assert matched == 4

    def test_sum_counter_label_filter_with_no_match_returns_zero(self):
        samples = [
            Sample("baldur_replay_outcomes_total", {"outcome": "success"}, 4.0),
        ]

        assert sum_counter(samples, {"outcome": "blocked"}) == 0

    def test_sum_counter_excludes_the_created_timestamp_sample(self):
        # A counter family also emits `_created` (a unix epoch). Summing it
        # would report ~1.7e9 replays.
        samples = [
            Sample("baldur_replay_outcomes_total", {"outcome": "success"}, 4.0),
            Sample("baldur_replay_outcomes_created", {"outcome": "success"}, 1.7e9),
        ]

        assert sum_counter(samples) == 4

    def test_sum_counter_truncates_a_fractional_total_to_an_integer(self):
        # Counters are integral in practice; the return type is int either way.
        samples = [Sample("baldur_thing_total", {}, 2.7)]

        assert sum_counter(samples) == 2

    def test_sum_counter_on_histogram_samples_returns_a_meaningless_zero(self):
        # Documented negative (D2 constraint): histogram samples are named
        # `_bucket` / `_count` / `_sum`, none of which end in `_total`, so this
        # function silently reports 0 for a busy histogram. Callers must route
        # histogram families to p95_from_buckets — this test pins WHY.
        samples = [
            *_bucket_samples(_REPLAY_DURATION, {"1.0": 5.0, "+Inf": 5.0}),
            Sample(f"{_REPLAY_DURATION}_count", {}, 5.0),
            Sample(f"{_REPLAY_DURATION}_sum", {}, 3.2),
        ]

        assert sum_counter(samples) == 0


class TestP95FromBucketsContract:
    """Interpolates the p95 the way ``histogram_quantile`` does, or answers None."""

    def test_p95_quantile_constant_is_the_ninety_fifth_percentile(self):
        # The reducer and every caller naming "p95" share this one constant.
        assert P95_QUANTILE == 0.95

    def test_p95_from_buckets_no_bucket_samples_returns_none(self):
        assert p95_from_buckets([]) is None

    def test_p95_from_buckets_counter_samples_return_none(self):
        # The mirror of the sum_counter negative above: a counter family fed
        # here has no `_bucket` samples at all, so nothing is fabricated.
        samples = [
            Sample(_REPLAY_DISPATCH, {"domain": "payments"}, 3.0),
        ]

        assert p95_from_buckets(samples) is None

    def test_p95_from_buckets_zero_observations_returns_none(self):
        # Registered histogram, nothing observed. None, never a rendered `0 ms`.
        samples = _bucket_samples(_REPLAY_DURATION, {"1.0": 0.0, "+Inf": 0.0})

        assert p95_from_buckets(samples) is None

    def test_p95_from_buckets_interpolates_inside_the_containing_bucket(self):
        # 10 observations, all in (1, 2]. rank = 0.95 * 10 = 9.5, which lands
        # 95% of the way through that bucket: 1 + (2-1) * (9.5/10).
        samples = _bucket_samples(
            _REPLAY_DURATION, {"1.0": 0.0, "2.0": 10.0, "+Inf": 10.0}
        )

        assert p95_from_buckets(samples) == pytest.approx(1.95)

    def test_p95_from_buckets_single_observation_reports_its_bucket(self):
        samples = _bucket_samples(
            _REPLAY_DURATION, {"0.1": 0.0, "0.5": 1.0, "+Inf": 1.0}
        )

        # rank = 0.95, inside the (0.1, 0.5] bucket which holds the one sample.
        assert p95_from_buckets(samples) == pytest.approx(0.1 + 0.4 * 0.95)

    def test_p95_from_buckets_rank_in_the_overflow_bucket_reports_the_top_bound(
        self,
    ):
        # 9 of 10 observations exceed the largest finite bound, so the rank
        # lands in `+Inf`. Prometheus convention: report the highest finite
        # bound rather than infinity.
        samples = _bucket_samples(
            _REPLAY_DURATION, {"1.0": 1.0, "5.0": 1.0, "+Inf": 10.0}
        )

        assert p95_from_buckets(samples) == 5.0

    def test_p95_from_buckets_all_overflow_without_a_finite_bound_returns_none(self):
        # Degenerate family with only `+Inf`: there is no finite bound to
        # report, so the honest answer is nothing.
        samples = _bucket_samples(_REPLAY_DURATION, {"+Inf": 5.0})

        assert p95_from_buckets(samples) is None

    def test_p95_from_buckets_merges_bucket_counts_across_label_sets(self):
        # Two domains, 5 observations each in (1, 2]. Merged, that is the same
        # 10-observation distribution as the interpolation case above — which
        # is the point: the console reports one number for the whole family.
        samples = [
            *_bucket_samples(
                _REPLAY_DURATION,
                {"1.0": 0.0, "2.0": 5.0, "+Inf": 5.0},
                domain="payments",
            ),
            *_bucket_samples(
                _REPLAY_DURATION,
                {"1.0": 0.0, "2.0": 5.0, "+Inf": 5.0},
                domain="orders",
            ),
        ]

        assert p95_from_buckets(samples) == pytest.approx(1.95)

    def test_p95_from_buckets_ignores_the_count_and_sum_samples(self):
        # `_count` / `_sum` carry no `le`; treating either as a bucket would
        # add a bound of +Inf and distort the merge.
        samples = [
            *_bucket_samples(_REPLAY_DURATION, {"1.0": 0.0, "2.0": 10.0, "+Inf": 10.0}),
            Sample(f"{_REPLAY_DURATION}_count", {}, 10.0),
            Sample(f"{_REPLAY_DURATION}_sum", {}, 17.5),
        ]

        assert p95_from_buckets(samples) == pytest.approx(1.95)

    def test_p95_from_buckets_reads_a_real_collected_histogram(self, isolated_registry):
        # The hand-built fixtures above assume prometheus emits cumulative
        # `_bucket` samples carrying an `le` label. This case proves that
        # assumption against the real client rather than restating it.
        histogram = Histogram(
            _REPLAY_DURATION,
            "Replay duration",
            buckets=(0.1, 0.5, 1.0, 2.0, 5.0),
            registry=isolated_registry,
        )
        for _ in range(19):
            histogram.observe(0.3)
        histogram.observe(4.0)

        collected = PrometheusAdapter().collect_families([_REPLAY_DURATION])
        p95 = p95_from_buckets(collected[_REPLAY_DURATION])

        # rank = 19.0 lands exactly at the (0.1, 0.5] bucket's top edge.
        assert p95 == pytest.approx(0.5)
