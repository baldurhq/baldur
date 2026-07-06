"""
Unit tests for Live Canary Evaluator.

Verified items:
- evaluator name contract: "live_canary"
- event_types contract: ["canary_metrics"]
- _calculate_confidence: confidence bands by request volume (0 / 1-99 / 100-499 / 500+)
- evaluate: pass/block determination from PassCriteria thresholds
  - minimum traffic floor (min_requests_required) — insufficient evidence blocks
  - error rate absolute threshold (error_rate_absolute_max)
  - error rate increase threshold (error_rate_increase_max)
  - P95 latency absolute increase (latency_p95_delta_ms)
  - P99 latency percentage increase (latency_p99_delta_pct)
- evaluate: division-by-zero safety when baseline_p99=0
- evaluate: result metric structure (baseline_metrics, candidate_metrics, delta)
- ConfigEvaluator Protocol conformance

Target: baldur.services.config_shadow.evaluators.live_canary
"""

import pytest

from baldur.models.canary import PassCriteria
from baldur.services.config_shadow.evaluators.live_canary import (
    LiveCanaryEvaluator,
)
from baldur.services.config_shadow.metrics_provider import (
    MockTimeSeriesProvider,
)
from baldur.services.config_shadow.models import EvaluationContext


def _make_provider(**scalars: float) -> MockTimeSeriesProvider:
    """Build a MockTimeSeriesProvider for tests."""
    provider = MockTimeSeriesProvider()
    provider._scalars = scalars
    return provider


def _make_context(
    service_name: str = "svc",
    time_window_seconds: int = 300,
) -> EvaluationContext:
    """Build an EvaluationContext for tests."""
    return EvaluationContext(
        baseline_config={"failure_threshold": 5},
        candidate_config={"failure_threshold": 3},
        service_name=service_name,
        time_window_seconds=time_window_seconds,
        baseline_labels={"track": "stable"},
        candidate_labels={"track": "canary"},
    )


class TestLiveCanaryEvaluatorContract:
    """LiveCanaryEvaluator design-contract values."""

    def test_name_is_live_canary(self):
        """evaluator name: 'live_canary'."""
        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        assert evaluator.name == "live_canary"

    def test_event_types_is_canary_metrics(self):
        """event_types: ['canary_metrics']."""
        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        assert evaluator.event_types == ["canary_metrics"]

    def test_default_pass_criteria_used_when_none(self):
        """pass_criteria=None uses the default PassCriteria."""
        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        assert evaluator._criteria.error_rate_absolute_max == 0.05
        assert evaluator._criteria.min_requests_required == 100

    def test_confidence_zero_requests_is_0_1(self):
        """0 requests: confidence 0.1."""
        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        conf, warnings = evaluator._calculate_confidence(0)
        assert conf == pytest.approx(0.1)
        assert len(warnings) == 1

    def test_confidence_below_min_requests_is_0_4(self):
        """1-99 requests (below min_requests=100): confidence 0.4."""
        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        conf, warnings = evaluator._calculate_confidence(50)
        assert conf == pytest.approx(0.4)
        assert len(warnings) == 1

    def test_confidence_min_to_5x_is_0_7(self):
        """100-499 requests (min*1 to below min*5): confidence 0.7."""
        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        conf, warnings = evaluator._calculate_confidence(300)
        assert conf == pytest.approx(0.7)
        assert len(warnings) == 0

    def test_confidence_5x_plus_is_0_95(self):
        """500+ requests (min*5): confidence 0.95."""
        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        conf, warnings = evaluator._calculate_confidence(500)
        assert conf == pytest.approx(0.95)
        assert len(warnings) == 0

    def test_implements_config_evaluator_protocol(self):
        """LiveCanaryEvaluator satisfies the ConfigEvaluator Protocol."""
        from baldur.services.config_shadow.evaluators import ConfigEvaluator

        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        assert isinstance(evaluator, ConfigEvaluator)


class TestLiveCanaryEvaluatorBehavior:
    """LiveCanaryEvaluator.evaluate behavior."""

    def test_healthy_canary_passes(self):
        """All metrics within thresholds → passed=True."""
        # Given
        provider = _make_provider(
            **{
                "svc:error_rate_agg": 0.02,
                "svc:request_count": 500,
                "svc:latency_p95": 100.0,
                "svc:latency_p99": 200.0,
            }
        )
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then
        assert result.passed is True
        assert result.evaluator_name == "live_canary"
        assert result.confidence_score == pytest.approx(0.95)

    def test_insufficient_traffic_blocks_even_when_metrics_healthy(self):
        """Below min_requests_required the verdict is a block — too little
        traffic is "not enough evidence", never a pass."""
        # Given — healthy-looking metrics but only 50 requests (< 100 required)
        provider = _make_provider(
            **{
                "svc:error_rate_agg": 0.01,
                "svc:request_count": 50,
                "svc:latency_p95": 100.0,
                "svc:latency_p99": 200.0,
            }
        )
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then
        assert result.passed is False
        assert "insufficient" in result.details.lower()
        assert "not enough evidence" in result.details.lower()

    def test_traffic_at_exact_minimum_passes(self):
        """Exactly min_requests_required is sufficient evidence (>= floor)."""
        provider = _make_provider(
            **{
                "svc:error_rate_agg": 0.01,
                "svc:request_count": 100,
                "svc:latency_p95": 100.0,
                "svc:latency_p99": 200.0,
            }
        )
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        result = evaluator.evaluate(context)

        assert result.passed is True

    def test_custom_min_requests_floor_applied(self):
        """A per-stage min_requests_required overrides the default floor."""
        provider = _make_provider(
            **{
                "svc:error_rate_agg": 0.01,
                "svc:request_count": 50,
                "svc:latency_p95": 100.0,
                "svc:latency_p99": 200.0,
            }
        )
        criteria = PassCriteria(min_requests_required=10)
        evaluator = LiveCanaryEvaluator(
            metrics_provider=provider, pass_criteria=criteria
        )
        context = _make_context()

        result = evaluator.evaluate(context)

        assert result.passed is True

    def test_error_rate_absolute_exceeds_threshold_fails(self):
        """Candidate error rate above the absolute threshold → passed=False."""
        # Given — candidate error > 0.05
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:error_rate_agg": 0.06,
            "svc:request_count": 500,
            "svc:latency_p95": 100.0,
            "svc:latency_p99": 200.0,
        }
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then
        assert result.passed is False
        assert "error rate" in result.details.lower()

    def test_error_rate_increase_exceeds_threshold_fails(self):
        """Candidate-baseline error delta above the increase threshold → passed=False."""
        # Given — Use label-differentiated keys so baseline and candidate
        # resolve to different error rates via MockTimeSeriesProvider.
        # baseline (track=stable) error_rate=0.01, candidate (track=canary)=0.03
        # → error_delta = 0.03 - 0.01 = 0.02, which exceeds the default
        #   PassCriteria.error_rate_increase_max of 0.01.
        # All other metrics (latency, request_count) are set to safe values
        # so that only the error-rate-increase check triggers the failure.
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:error_rate_agg:track=stable": 0.01,
            "svc:error_rate_agg:track=canary": 0.03,
            "svc:request_count:track=canary": 500,
            "svc:latency_p95:track=stable": 100.0,
            "svc:latency_p95:track=canary": 100.0,
            "svc:latency_p99:track=stable": 200.0,
            "svc:latency_p99:track=canary": 200.0,
        }
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then — error_delta=0.02 > default threshold 0.01
        assert result.passed is False
        assert "increase" in result.details.lower()

    def test_p95_latency_delta_exceeds_threshold_fails(self):
        """P95 latency delta above the threshold → passed=False."""
        # Given — Use label-differentiated keys to set distinct P95 latencies
        # for baseline and candidate.
        # baseline (track=stable) P95=100ms, candidate (track=canary) P95=160ms
        # → p95_delta = 160 - 100 = 60ms, which exceeds the default
        #   PassCriteria.latency_p95_delta_ms of 50ms.
        # Error rates are equal (0.01) and P99 values are identical (200ms)
        # so that only the P95 latency delta check triggers the failure.
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:error_rate_agg:track=stable": 0.01,
            "svc:error_rate_agg:track=canary": 0.01,
            "svc:request_count:track=canary": 500,
            "svc:latency_p95:track=stable": 100.0,
            "svc:latency_p95:track=canary": 160.0,
            "svc:latency_p99:track=stable": 200.0,
            "svc:latency_p99:track=canary": 200.0,
        }
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then — p95_delta=60ms > default threshold 50ms
        assert result.passed is False
        assert "p95" in result.details.lower()

    def test_p99_latency_pct_exceeds_threshold_fails(self):
        """P99 latency percentage increase above the threshold → passed=False."""
        # Given — Use label-differentiated keys to set distinct P99 latencies
        # for baseline and candidate.
        # baseline (track=stable) P99=200ms, candidate (track=canary) P99=260ms
        # → p99_pct = (260 - 200) / 200 = 0.30 (30%), which exceeds the default
        #   PassCriteria.latency_p99_delta_pct of 0.20 (20%).
        # Error rates are equal (0.01) and P95 values are identical (100ms)
        # so that only the P99 latency percentage check triggers the failure.
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:error_rate_agg:track=stable": 0.01,
            "svc:error_rate_agg:track=canary": 0.01,
            "svc:request_count:track=canary": 500,
            "svc:latency_p95:track=stable": 100.0,
            "svc:latency_p95:track=canary": 100.0,
            "svc:latency_p99:track=stable": 200.0,
            "svc:latency_p99:track=canary": 260.0,
        }
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then — p99_pct=30% > default threshold 20%
        assert result.passed is False
        assert "p99" in result.details.lower()

    def test_baseline_p99_zero_skips_pct_check(self):
        """baseline P99=0 skips the percentage check (no division by zero)."""
        # Given
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:error_rate_agg": 0.01,
            "svc:request_count": 500,
            "svc:latency_p95": 100.0,
            "svc:latency_p99": 0.0,
        }
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then
        assert result.passed is True
        assert result.delta["p99_delta_pct"] == 0.0

    def test_result_contains_expected_metric_keys(self):
        """The result includes baseline_metrics, candidate_metrics, delta keys."""
        # Given
        provider = _make_provider(
            **{
                "svc:error_rate_agg": 0.02,
                "svc:request_count": 200,
                "svc:latency_p95": 50.0,
                "svc:latency_p99": 100.0,
            }
        )
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then — baseline_metrics
        assert "error_rate" in result.baseline_metrics
        assert "latency_p95_ms" in result.baseline_metrics
        assert "latency_p99_ms" in result.baseline_metrics

        # Then — candidate_metrics
        assert "error_rate" in result.candidate_metrics
        assert "request_count" in result.candidate_metrics
        assert "latency_p95_ms" in result.candidate_metrics
        assert "latency_p99_ms" in result.candidate_metrics

        # Then — delta
        assert "error_rate_delta" in result.delta
        assert "p95_delta_ms" in result.delta
        assert "p99_delta_pct" in result.delta

    def test_multiple_failures_all_reported(self):
        """Multiple simultaneous threshold violations all appear in details."""
        # Given — Set up label-differentiated keys where ALL four threshold
        # checks fail simultaneously. This verifies that the evaluator does
        # not short-circuit on the first failure but reports every violation.
        #
        # baseline (track=stable): error_rate=0.01, P95=100ms, P99=200ms
        # candidate (track=canary): error_rate=0.10, P95=200ms, P99=300ms
        #
        # Expected violations:
        #   1) error_rate_absolute: 0.10 > 0.05 (default threshold)
        #   2) error_rate_increase: 0.10 - 0.01 = 0.09 > 0.01 (default threshold)
        #   3) p95_delta: 200 - 100 = 100ms > 50ms (default threshold)
        #   4) p99_pct: (300 - 200) / 200 = 50% > 20% (default threshold)
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:error_rate_agg:track=stable": 0.01,
            "svc:error_rate_agg:track=canary": 0.10,
            "svc:request_count:track=canary": 500,
            "svc:latency_p95:track=stable": 100.0,
            "svc:latency_p95:track=canary": 200.0,
            "svc:latency_p99:track=stable": 200.0,
            "svc:latency_p99:track=canary": 300.0,
        }
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then — All four failure reasons must appear in details string
        assert result.passed is False
        details_lower = result.details.lower()
        assert "error rate" in details_lower
        assert "increase" in details_lower
        assert "p95" in details_lower
        assert "p99" in details_lower

    def test_custom_pass_criteria_applied(self):
        """A custom PassCriteria is used for the determination."""
        # Given — very lenient criteria
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:error_rate_agg": 0.5,
            "svc:request_count": 500,
            "svc:latency_p95": 100.0,
            "svc:latency_p99": 200.0,
        }
        lenient_criteria = PassCriteria(
            error_rate_absolute_max=1.0,
            error_rate_increase_max=1.0,
            latency_p95_delta_ms=10000.0,
            latency_p99_delta_pct=10.0,
        )
        evaluator = LiveCanaryEvaluator(
            metrics_provider=provider, pass_criteria=lenient_criteria
        )
        context = _make_context()

        # When
        result = evaluator.evaluate(context)

        # Then
        assert result.passed is True

    def test_passed_result_contains_healthy_summary(self):
        """A passing result includes a healthy summary in details."""
        provider = _make_provider(
            **{
                "svc:error_rate_agg": 0.01,
                "svc:request_count": 500,
                "svc:latency_p95": 50.0,
                "svc:latency_p99": 100.0,
            }
        )
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        result = evaluator.evaluate(context)

        assert result.passed is True
        assert "healthy" in result.details.lower()


class TestLiveCanaryConfidenceBoundaryBehavior:
    """_calculate_confidence boundary behavior."""

    def test_boundary_at_min_requests_transitions_to_0_7(self):
        """Exactly min_requests transitions 0.4 → 0.7."""
        provider = MockTimeSeriesProvider()
        criteria = PassCriteria(min_requests_required=100)
        evaluator = LiveCanaryEvaluator(
            metrics_provider=provider, pass_criteria=criteria
        )

        # 99 requests: 0.4
        conf_below, _ = evaluator._calculate_confidence(99)
        assert conf_below == pytest.approx(0.4)

        # 100 requests: 0.7
        conf_at, _ = evaluator._calculate_confidence(100)
        assert conf_at == pytest.approx(0.7)

    def test_boundary_at_5x_min_requests_transitions_to_0_95(self):
        """Exactly min_requests*5 transitions 0.7 → 0.95."""
        provider = MockTimeSeriesProvider()
        criteria = PassCriteria(min_requests_required=100)
        evaluator = LiveCanaryEvaluator(
            metrics_provider=provider, pass_criteria=criteria
        )

        # 499 requests: 0.7
        conf_below, _ = evaluator._calculate_confidence(499)
        assert conf_below == pytest.approx(0.7)

        # 500 requests: 0.95
        conf_at, _ = evaluator._calculate_confidence(500)
        assert conf_at == pytest.approx(0.95)

    def test_low_volume_warning_included_below_min(self):
        """Below min_requests a Low request volume warning is included."""
        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)

        _, warnings = evaluator._calculate_confidence(50)
        assert len(warnings) == 1
        assert "Low request volume" in warnings[0]

    def test_no_warning_at_or_above_min(self):
        """At or above min_requests there is no warning."""
        provider = MockTimeSeriesProvider()
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)

        _, warnings = evaluator._calculate_confidence(100)
        assert len(warnings) == 0


class TestLiveCanaryEvaluatorEdgeCaseBehavior:
    """LiveCanaryEvaluator edge-case behavior."""

    def test_zero_requests_very_low_confidence(self):
        """0 requests: evaluation still runs, confidence=0.1."""
        provider = _make_provider(
            **{
                "svc:error_rate_agg": 0.0,
                "svc:request_count": 0,
                "svc:latency_p95": 0.0,
                "svc:latency_p99": 0.0,
            }
        )
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        result = evaluator.evaluate(context)

        assert result.confidence_score == pytest.approx(0.1)
        assert len(result.warnings) >= 1

    def test_all_metrics_zero_blocked_as_insufficient_evidence(self):
        """All-zero metrics (an empty window) are NOT a pass: zero requests
        means no evidence, and the traffic floor blocks the verdict."""
        provider = MockTimeSeriesProvider()
        # all scalars default to 0.0 — including request_count
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        result = evaluator.evaluate(context)

        assert result.passed is False
        assert "insufficient" in result.details.lower()

    def test_all_threshold_metrics_zero_with_traffic_passes(self):
        """Zero error/latency metrics WITH sufficient traffic pass — the
        floor gates evidence volume, not metric values."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:request_count": 500,
        }
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        result = evaluator.evaluate(context)

        assert result.passed is True

    def test_error_rate_at_exact_threshold_passes(self):
        """An error rate exactly at the threshold (0.05) passes (strict >)."""
        provider = MockTimeSeriesProvider()
        provider._scalars = {
            "svc:error_rate_agg": 0.05,
            "svc:request_count": 500,
            "svc:latency_p95": 100.0,
            "svc:latency_p99": 200.0,
        }
        evaluator = LiveCanaryEvaluator(metrics_provider=provider)
        context = _make_context()

        result = evaluator.evaluate(context)

        # 0.05 is NOT > 0.05, so it should pass (on error_rate_absolute_max)
        # error_delta = 0.0, also passes
        assert result.passed is True
