"""Unit tests for settings_recommendation.pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.core.decision_engine import (
    AdjustmentDecision,
    AdjustmentPriority,
    DecisionEngine,
)
from baldur.services.settings_recommendation.models import (
    RecommendationSource,
)
from baldur.services.settings_recommendation.pipeline import (
    LOWER_IS_BETTER,
    METRIC_TO_PARAM,
    RecommendationPipeline,
)

from .conftest import _make_item

# ---------------------------------------------------------------------------
# Contract Tests
# ---------------------------------------------------------------------------


class TestPipelineConstantsContract:
    """Design contract values for pipeline constants."""

    def test_metric_to_param_mapping_count(self):
        """Design contract: 5 metric→parameter mappings from DEFAULT_RULES."""
        assert len(METRIC_TO_PARAM) == 5

    def test_metric_to_param_values(self):
        """Design contract: metric→parameter mappings match DEFAULT_RULES."""
        assert METRIC_TO_PARAM["p99_latency_ms"] == "timeout_ms"
        assert METRIC_TO_PARAM["retry_exhausted_rate"] == "retry_count"
        assert METRIC_TO_PARAM["error_rate"] == "circuit_breaker_threshold"
        assert METRIC_TO_PARAM["retry_collision_rate"] == "jitter_range"
        assert METRIC_TO_PARAM["throttle_rate"] == "rate_limit_rps"

    def test_lower_is_better_metrics(self):
        """Design contract: 5 metrics where increase = degradation."""
        assert "error_rate" in LOWER_IS_BETTER
        assert "retry_exhaustion_rate" in LOWER_IS_BETTER
        assert "throttle_rate" in LOWER_IS_BETTER
        assert "p99_latency_ms" in LOWER_IS_BETTER
        assert "cb_open_ratio" in LOWER_IS_BETTER


# ---------------------------------------------------------------------------
# Behavior Tests
# ---------------------------------------------------------------------------


class TestStepAnalyzeRulesBehavior:
    """step_analyze_rules conversion behavior."""

    def test_converts_decisions_to_recommendation_items(self):
        """DecisionEngine decisions are converted to RecommendationItems."""
        # Given
        mock_engine = MagicMock(spec=DecisionEngine)
        mock_engine.analyze.return_value = [
            AdjustmentDecision(
                parameter="timeout_ms",
                current_value=5000.0,
                suggested_value=6000.0,
                reason="latency high",
                confidence=0.9,
                priority=AdjustmentPriority.HIGH,
                metric_snapshot={"p99_latency_ms": 4500.0},
            )
        ]
        pipeline = RecommendationPipeline(decision_engine=mock_engine)

        # When
        items = pipeline.step_analyze_rules({"p99_latency_ms": 4500.0})

        # Then
        assert len(items) == 1
        assert items[0].parameter == "timeout_ms"
        assert items[0].source == RecommendationSource.RULE_BASED
        assert items[0].confidence == 0.9
        assert items[0].priority == AdjustmentPriority.HIGH

    def test_passes_prediction_context_to_engine(self):
        """prediction_context is forwarded to DecisionEngine.analyze()."""
        mock_engine = MagicMock(spec=DecisionEngine)
        mock_engine.analyze.return_value = []
        pipeline = RecommendationPipeline(decision_engine=mock_engine)
        ctx = {"trend_slope": 0.1}

        pipeline.step_analyze_rules({"error_rate": 0.05}, prediction_context=ctx)

        mock_engine.analyze.assert_called_once_with(
            {"error_rate": 0.05},
            prediction_context=ctx,
        )

    def test_returns_empty_when_no_engine(self):
        """Without DecisionEngine, returns empty list."""
        pipeline = RecommendationPipeline(decision_engine=None)
        assert pipeline.step_analyze_rules({"error_rate": 0.05}) == []


class TestStepMergeBehavior:
    """step_merge dedup, anomaly boost, limit behavior."""

    def test_deduplicates_same_parameter_keeps_highest_confidence(self):
        """Same parameter from multiple sources keeps highest confidence."""
        pipeline = RecommendationPipeline()
        items = [
            _make_item(confidence=0.7),
            _make_item(confidence=0.9),
        ]

        merged = pipeline.step_merge(items)

        assert len(merged) == 1
        assert merged[0].confidence == 0.9

    def test_anomaly_boost_increases_confidence(self):
        """Anomaly context boosts confidence of matching items."""
        pipeline = RecommendationPipeline()
        items = [
            _make_item(
                confidence=0.8,
                metric_evidence={"error_rate": 0.05},
            )
        ]
        anomaly_context = {
            "detected": {
                "error_rate": {"score": -0.8, "confidence": 0.8, "value": 0.05},
            }
        }

        merged = pipeline.step_merge(items, anomaly_context=anomaly_context)

        # boost = 0.8 * 0.15 = 0.12; original 0.8 + 0.12 = 0.92
        assert merged[0].confidence == pytest.approx(0.92)
        assert merged[0].metric_evidence.get("anomaly_detected") == "error_rate"

    def test_anomaly_boost_capped_at_one(self):
        """Confidence after anomaly boost does not exceed 1.0."""
        pipeline = RecommendationPipeline()
        items = [
            _make_item(
                confidence=0.95,
                metric_evidence={"error_rate": 0.05},
            )
        ]
        anomaly_context = {
            "detected": {
                "error_rate": {"score": -0.9, "confidence": 1.0, "value": 0.1},
            }
        }

        merged = pipeline.step_merge(items, anomaly_context=anomaly_context)
        assert merged[0].confidence <= 1.0

    def test_max_changes_per_cycle_limits_output(self):
        """Output limited to max_changes_per_cycle."""
        pipeline = RecommendationPipeline(max_changes_per_cycle=2)
        items = [
            _make_item(parameter=f"param_{i}", confidence=0.9 - i * 0.1)
            for i in range(5)
        ]

        merged = pipeline.step_merge(items)
        assert len(merged) == 2

    def test_sort_by_priority_critical_first(self):
        """Items sorted by priority: CRITICAL > MEDIUM > LOW."""
        pipeline = RecommendationPipeline(max_changes_per_cycle=10)
        items = [
            _make_item(parameter="a", priority=AdjustmentPriority.LOW),
            _make_item(parameter="b", priority=AdjustmentPriority.CRITICAL),
            _make_item(parameter="c", priority=AdjustmentPriority.MEDIUM),
        ]

        merged = pipeline.step_merge(items)
        assert merged[0].priority == AdjustmentPriority.CRITICAL
        assert merged[1].priority == AdjustmentPriority.MEDIUM
        assert merged[2].priority == AdjustmentPriority.LOW

    def test_empty_items_returns_empty(self):
        """Empty input returns empty output."""
        pipeline = RecommendationPipeline()
        assert pipeline.step_merge([]) == []


class TestStepPropagateBehavior:
    """step_propagate dependency cascade behavior."""

    def test_adds_cascade_items_from_dependency_graph(self):
        """Cascade items are added from DependencyGraph.propagate()."""
        from baldur.core.settings_dependency import SettingsDependencyGraph

        mock_graph = MagicMock(spec=SettingsDependencyGraph)
        mock_graph.propagate.return_value = {
            "timeout_ms": 6000.0,
            "backoff_max_ms": 7000.0,
        }
        pipeline = RecommendationPipeline(dependency_graph=mock_graph)
        items = [_make_item(parameter="timeout_ms", recommended_value=6000.0)]
        current_values = {"timeout_ms": 5000.0, "backoff_max_ms": 5000.0}

        result = pipeline.step_propagate(items, current_values)

        cascade_items = [i for i in result if i.is_cascade]
        assert len(cascade_items) == 1
        assert cascade_items[0].parameter == "backoff_max_ms"
        assert cascade_items[0].source == RecommendationSource.DEPENDENCY_CASCADE

    def test_no_graph_returns_items_unchanged(self):
        """Without dependency_graph, items returned as-is."""
        pipeline = RecommendationPipeline(dependency_graph=None)
        items = [_make_item()]
        result = pipeline.step_propagate(items, {})
        assert result == items


class TestPipelineRunBehavior:
    """Full pipeline.run() integration behavior."""

    def test_run_executes_all_steps_in_sequence(self):
        """run() produces items through all 4 steps."""
        # Given
        mock_engine = MagicMock(spec=DecisionEngine)
        mock_engine.analyze.return_value = [
            AdjustmentDecision(
                parameter="timeout_ms",
                current_value=5000.0,
                suggested_value=6000.0,
                reason="high latency",
                confidence=0.85,
                priority=AdjustmentPriority.MEDIUM,
            )
        ]
        pipeline = RecommendationPipeline(
            decision_engine=mock_engine,
            mode="rule_based",
        )

        # When
        items, anomaly_ctx = pipeline.run(
            metrics={"p99_latency_ms": 4500.0},
            ml_context={"current_values": {}},
        )

        # Then
        assert len(items) == 1
        assert items[0].parameter == "timeout_ms"


class TestIsMetricWorseningBehavior:
    """_is_metric_worsening helper behavior."""

    def test_error_rate_increase_is_worsening(self):
        """error_rate increase = degradation (lower is better)."""
        assert (
            RecommendationPipeline._is_metric_worsening("error_rate", 0.01, 0.05)
            is True
        )

    def test_error_rate_decrease_is_not_worsening(self):
        """error_rate decrease = improvement."""
        assert (
            RecommendationPipeline._is_metric_worsening("error_rate", 0.05, 0.01)
            is False
        )

    def test_throughput_decrease_is_worsening(self):
        """throughput decrease = degradation (higher is better)."""
        assert (
            RecommendationPipeline._is_metric_worsening("throughput", 1000, 500) is True
        )

    def test_throughput_increase_is_not_worsening(self):
        """throughput increase = improvement."""
        assert (
            RecommendationPipeline._is_metric_worsening("throughput", 500, 1000)
            is False
        )
