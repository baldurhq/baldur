"""Edge-case contract tests for the shared PromQL metric-name tokenizer.

``_promql.metric_tokens`` is the only non-trivial executable logic the
metric-drift guards add: a regex approximation that strips label clauses,
label matchers, range durations, and string literals, then subtracts the
reserved PromQL function/keyword set. Both G43 (dashboard panels) and G48
(alert rules) depend on it, so a silent regression in any strip stage would
weaken BOTH guards at once.

The guards themselves only exercise the tokenizer on the metric exprs that
happen to ship today — incidental coverage, NOT a pinned contract. These tests
pin the contract directly, targeting the actual failure modes of a regex
tokenizer: values that live inside a label matcher, a string literal, a
duration, or a ``by (...)`` clause MUST NOT leak out as fake metric tokens, and
reserved functions/keywords MUST NOT be mistaken for metric names. If a strip
regex breaks, the relevant test below turns red immediately instead of waiting
for a future shipped expr to coincidentally expose the hole.

These complement (do not replace) the in-file guard-of-the-guard tests in
``test_grafana_dashboard_metric_drift.py`` / ``test_prometheus_alert_metric_drift.py``.
"""

from __future__ import annotations

from tests.architecture._promql import (
    PROMQL_RESERVED,
    metric_tokens,
    unregistered_tokens,
)


class TestMetricTokensBehavior:
    """The tokenizer's strip stages, verified against the documented contract."""

    def test_metric_tokens_strips_label_matcher_values(self) -> None:
        # Given an expr whose label matcher value LOOKS like a baldur_ metric
        expr = 'baldur_retry_outcomes_total{outcome="success", domain="payments"}'
        # When tokenizing
        tokens = metric_tokens(expr)
        # Then only the series name survives; matcher keys/values do not leak
        assert tokens == {"baldur_retry_outcomes_total"}

    def test_metric_tokens_strips_string_literals(self) -> None:
        # Given a string literal arg that looks like a metric name
        expr = 'label_replace(baldur_http_requests_total, "svc", "baldur_fake_name", "src", "")'
        # When tokenizing
        tokens = metric_tokens(expr)
        # Then the real metric is extracted but the quoted look-alike is not
        assert "baldur_http_requests_total" in tokens
        assert "baldur_fake_name" not in tokens

    def test_metric_tokens_strips_range_durations(self) -> None:
        # Given a range vector with a duration
        expr = "rate(baldur_circuit_breaker_trips_total[15m])"
        # When tokenizing
        tokens = metric_tokens(expr)
        # Then the metric resolves and the duration literal does not leak
        assert tokens == {"baldur_circuit_breaker_trips_total"}

    def test_metric_tokens_excludes_by_label_list(self) -> None:
        # Given a `by (...)` grouping clause naming label keys
        expr = "sum(rate(baldur_retry_outcomes_total[5m])) by (domain)"
        # When tokenizing
        tokens = metric_tokens(expr)
        # Then the grouping label `domain` is not mistaken for a metric
        assert tokens == {"baldur_retry_outcomes_total"}

    def test_metric_tokens_excludes_reserved_functions(self) -> None:
        # Given an expr wrapping a metric in reserved functions + a `by (le)` clause
        expr = (
            "histogram_quantile(0.95, "
            "sum(rate(baldur_http_request_duration_seconds_bucket[5m])) by (le))"
        )
        # When tokenizing
        tokens = metric_tokens(expr)
        # Then only the metric remains; no function name or `le` keyword leaks
        assert tokens == {"baldur_http_request_duration_seconds_bucket"}

    def test_metric_tokens_extracts_both_sides_of_a_ratio(self) -> None:
        # Given a ratio referencing two distinct series
        expr = (
            "sum(rate(baldur_retry_outcomes_total[5m])) "
            "/ sum(rate(baldur_http_requests_total[5m]))"
        )
        # When tokenizing
        tokens = metric_tokens(expr)
        # Then both metric names are captured
        assert tokens == {"baldur_retry_outcomes_total", "baldur_http_requests_total"}

    def test_metric_tokens_empty_expr_returns_empty_set(self) -> None:
        assert metric_tokens("") == set()

    def test_metric_tokens_constant_only_expr_returns_empty_set(self) -> None:
        # Numeric thresholds are not identifiers and must yield no metric tokens
        assert metric_tokens("0.95 > 0.8") == set()


class TestUnregisteredTokensBehavior:
    """The set-difference helper both guards assert on."""

    def test_unregistered_tokens_flags_only_absent_series(self) -> None:
        # Given a registry containing one of two referenced series
        registered = frozenset({"baldur_retry_outcomes_total"})
        expr = "rate(baldur_retry_outcomes_total[5m]) / rate(baldur_missing_total[5m])"
        # When computing unregistered tokens
        missing = unregistered_tokens(expr, registered)
        # Then only the absent series is reported
        assert missing == {"baldur_missing_total"}

    def test_unregistered_tokens_all_registered_returns_empty(self) -> None:
        registered = frozenset(
            {"baldur_retry_outcomes_total", "baldur_http_requests_total"}
        )
        expr = (
            "sum(rate(baldur_retry_outcomes_total[5m])) "
            "/ sum(rate(baldur_http_requests_total[5m]))"
        )
        assert unregistered_tokens(expr, registered) == set()


class TestPromqlReservedContract:
    """The reserved set must classify core PromQL tokens, or every guard breaks."""

    def test_promql_reserved_contains_core_tokens_across_categories(self) -> None:
        # A wholesale truncation of the reserved set would make functions/keywords
        # resolve as fake metric names and red-fail every guard. Pin a
        # representative member from each category the tokenizer relies on.
        required = {
            "sum",  # aggregation operator
            "rate",  # range function
            "increase",  # range function
            "histogram_quantile",  # histogram function
            "by",  # grouping keyword
            "without",  # grouping keyword
            "le",  # histogram bucket label keyword
        }
        assert required <= PROMQL_RESERVED
