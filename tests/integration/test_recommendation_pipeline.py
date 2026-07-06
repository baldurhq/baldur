"""Integration test: Settings Recommendation Pipeline E2E (mock-based).

Tests the full lifecycle: generate → validate → apply with multiple
components working together (Service + Pipeline + PlanStore).
No infrastructure dependencies — all external services mocked.

Test Categories:
    A. E2E Lifecycle:
        - generate → validate → apply full path
        - constraint violation rejection
        - auto_apply transitions
    B. Shadow Evaluation Integration:
        - VALIDATING status with evaluation_id
        - shadow skip produces VALIDATED
    C. Plan History Integration:
        - plan storage and retrieval
        - recent plans filtering

Note: All tests use MagicMock — no DB/Redis dependency.
      This enables parallel test execution with pytest-xdist.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.core.decision_engine import AdjustmentDecision, AdjustmentPriority
from baldur.services.settings_recommendation.models import (
    RecommendationStatus,
)
from baldur.services.settings_recommendation.service import (
    SettingsRecommendationService,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_decision_engine():
    """DecisionEngine that returns one timeout_ms adjustment."""
    from baldur.core.decision_engine import DecisionEngine

    engine = MagicMock(spec=DecisionEngine)
    engine.analyze.return_value = [
        AdjustmentDecision(
            parameter="timeout_ms",
            current_value=5000.0,
            suggested_value=6000.0,
            reason="P99 latency high",
            confidence=0.85,
            priority=AdjustmentPriority.HIGH,
            metric_snapshot={"p99_latency_ms": 4500.0},
        )
    ]
    return engine


@pytest.fixture
def mock_constraint_engine_pass():
    """ConstraintEngine that passes all validations."""
    from baldur.core.constraint_engine import ConstraintEngine, ConstraintResult

    engine = MagicMock(spec=ConstraintEngine)
    result = MagicMock(spec=ConstraintResult)
    result.passed = True
    result.violations = []
    engine.validate.return_value = result
    return engine


@pytest.fixture
def mock_constraint_engine_fail():
    """ConstraintEngine that fails validation."""
    from baldur.core.constraint_engine import ConstraintEngine, ConstraintResult

    engine = MagicMock(spec=ConstraintEngine)
    result = MagicMock(spec=ConstraintResult)
    result.passed = False
    result.violations = [MagicMock()]
    engine.validate.return_value = result
    return engine


def _create_service(
    decision_engine=None,
    constraint_engine=None,
    shadow_service=None,
    canary_service=None,
) -> SettingsRecommendationService:
    svc = SettingsRecommendationService(
        decision_engine=decision_engine,
        constraint_engine=constraint_engine,
        shadow_service=shadow_service,
        canary_service=canary_service,
    )
    svc._settings.shadow_required = False
    svc._settings.canary_required = False
    svc._settings.auto_apply = False
    return svc


# ---------------------------------------------------------------------------
# E2E Lifecycle Tests
# ---------------------------------------------------------------------------


class TestRecommendationPipelineE2E:
    """Full pipeline lifecycle: generate → validate → apply."""

    def test_generate_validate_apply_lifecycle(
        self, mock_decision_engine, mock_constraint_engine_pass
    ):
        """Complete lifecycle: GENERATED → VALIDATED → APPLIED."""
        svc = _create_service(
            decision_engine=mock_decision_engine,
            constraint_engine=mock_constraint_engine_pass,
        )

        # Phase 1: Generate
        plan = svc.generate_recommendations(metrics={"p99_latency_ms": 4500.0})
        assert plan.status == RecommendationStatus.GENERATED
        assert plan.parameter_count >= 1

        # Phase 2: Validate
        plan = svc.validate_plan(plan)
        assert plan.status == RecommendationStatus.VALIDATED
        mock_constraint_engine_pass.validate.assert_called_once()

        # Phase 3: Apply
        plan = svc.apply_plan(plan)
        assert plan.status == RecommendationStatus.APPLIED
        assert plan.applied_at is not None

        # Verify stored plan reflects final state
        stored = svc.get_plan(plan.plan_id)
        assert stored.status == RecommendationStatus.APPLIED

    def test_constraint_violation_rejects_plan(
        self, mock_decision_engine, mock_constraint_engine_fail
    ):
        """Constraint violation stops lifecycle at REJECTED."""
        svc = _create_service(
            decision_engine=mock_decision_engine,
            constraint_engine=mock_constraint_engine_fail,
        )

        plan = svc.generate_recommendations(metrics={"p99_latency_ms": 4500.0})
        plan = svc.validate_plan(plan)

        assert plan.status == RecommendationStatus.REJECTED
        assert plan.constraint_result is not None

    def test_run_full_pipeline_generates_validated_plan(
        self, mock_decision_engine, mock_constraint_engine_pass
    ):
        """run_full_pipeline produces VALIDATED plan when auto_apply=False."""
        svc = _create_service(
            decision_engine=mock_decision_engine,
            constraint_engine=mock_constraint_engine_pass,
        )

        plan = svc.run_full_pipeline(metrics={"p99_latency_ms": 4500.0})

        assert plan is not None
        assert plan.status == RecommendationStatus.VALIDATED

    def test_run_full_pipeline_with_auto_apply(
        self, mock_decision_engine, mock_constraint_engine_pass
    ):
        """run_full_pipeline with auto_apply=True produces APPLIED plan."""
        svc = _create_service(
            decision_engine=mock_decision_engine,
            constraint_engine=mock_constraint_engine_pass,
        )
        svc._settings.auto_apply = True

        plan = svc.run_full_pipeline(metrics={"p99_latency_ms": 4500.0})

        assert plan is not None
        assert plan.status == RecommendationStatus.APPLIED


class TestShadowEvaluationIntegration:
    """Shadow evaluation async flow integration."""

    def test_shadow_required_creates_validating_plan(self, mock_decision_engine):
        """Shadow required → VALIDATING status with evaluation_id."""
        from baldur.services.config_shadow.service import ShadowEvaluatorService

        mock_shadow = MagicMock(spec=ShadowEvaluatorService)
        mock_eval = MagicMock()
        mock_eval.evaluation_id = "eval-abc"
        mock_shadow.submit_evaluation.return_value = mock_eval

        svc = _create_service(
            decision_engine=mock_decision_engine,
            shadow_service=mock_shadow,
        )
        svc._settings.shadow_required = True

        plan = svc.generate_recommendations(metrics={"p99_latency_ms": 4500.0})
        plan = svc.validate_plan(plan)

        assert plan.status == RecommendationStatus.VALIDATING
        assert plan.shadow_evaluation_id == "eval-abc"
        mock_shadow.submit_evaluation.assert_called_once()

    def test_check_pending_plans_completes_shadow_evaluation(
        self, mock_decision_engine
    ):
        """Shadow completion transitions VALIDATING → VALIDATED."""
        # Given: shadow service
        from baldur.services.config_shadow.service import ShadowEvaluatorService

        mock_shadow = MagicMock(spec=ShadowEvaluatorService)
        mock_submit_eval = MagicMock()
        mock_submit_eval.evaluation_id = "eval-xyz"
        mock_shadow.submit_evaluation.return_value = mock_submit_eval

        svc = _create_service(
            decision_engine=mock_decision_engine,
            shadow_service=mock_shadow,
        )
        svc._settings.shadow_required = True

        # Create VALIDATING plan
        plan = svc.generate_recommendations(metrics={"p99_latency_ms": 4500.0})
        plan = svc.validate_plan(plan)
        assert plan.status == RecommendationStatus.VALIDATING

        # Simulate shadow completion
        from baldur.services.config_shadow.models import EvaluationStatus

        mock_completed_eval = MagicMock()
        mock_completed_eval.status = EvaluationStatus.COMPLETED
        mock_completed_eval.report = MagicMock()
        mock_completed_eval.report.passed = True
        mock_shadow.get_evaluation.return_value = mock_completed_eval

        # When: check pending
        updated = svc.check_pending_plans()

        # Then
        assert len(updated) == 1
        assert updated[0].status == RecommendationStatus.VALIDATED


class TestPlanHistoryIntegration:
    """Plan storage and query integration."""

    def test_recommendation_history_filtered_by_parameter(self, mock_decision_engine):
        """get_recommendation_history filters by parameter correctly."""
        from baldur.services.settings_recommendation.pipeline import (
            RecommendationPipeline,
        )

        svc = _create_service(decision_engine=mock_decision_engine)
        svc._pipeline = MagicMock(spec=RecommendationPipeline)

        # Generate plans with different parameters
        from baldur.services.settings_recommendation.models import (
            RecommendationItem,
            RecommendationSource,
        )

        svc._pipeline.run.return_value = (
            [
                RecommendationItem(
                    parameter="timeout_ms",
                    current_value=5000.0,
                    recommended_value=6000.0,
                    source=RecommendationSource.RULE_BASED,
                    confidence=0.9,
                    expected_improvement=0.2,
                    reason="test",
                    priority=AdjustmentPriority.MEDIUM,
                )
            ],
            {},
        )
        svc.generate_recommendations(metrics={"error_rate": 0.05})

        svc._pipeline.run.return_value = (
            [
                RecommendationItem(
                    parameter="retry_count",
                    current_value=3.0,
                    recommended_value=4.0,
                    source=RecommendationSource.RULE_BASED,
                    confidence=0.8,
                    expected_improvement=0.1,
                    reason="test2",
                    priority=AdjustmentPriority.MEDIUM,
                )
            ],
            {},
        )
        svc.generate_recommendations(metrics={"error_rate": 0.05})

        # Filter by timeout_ms parameter
        history = svc.get_recommendation_history(parameter="timeout_ms")
        assert len(history) == 1
        assert any(i.parameter == "timeout_ms" for i in history[0].items)
