"""Unit tests for settings_recommendation.service."""

from __future__ import annotations

from unittest.mock import MagicMock

from baldur.core.decision_engine import DecisionEngine
from baldur.services.settings_recommendation.models import (
    RecommendationPlan,
    RecommendationStatus,
)
from baldur.services.settings_recommendation.service import (
    SettingsRecommendationService,
    get_settings_recommendation_service,
    reset_settings_recommendation_service,
)

from .conftest import _make_item


def _make_service(**overrides) -> SettingsRecommendationService:
    """Create a service with mock dependencies."""
    defaults = {
        "decision_engine": MagicMock(spec=DecisionEngine),
        "constraint_engine": None,
        "shadow_service": None,
        "canary_service": None,
    }
    defaults.update(overrides)
    svc = SettingsRecommendationService(**defaults)
    # Override settings for test
    svc._settings.shadow_required = False
    svc._settings.canary_required = False
    svc._settings.auto_apply = False
    return svc


# ---------------------------------------------------------------------------
# Behavior Tests
# ---------------------------------------------------------------------------


class TestGenerateRecommendationsBehavior:
    """generate_recommendations behavior."""

    def test_generates_plan_with_generated_status(self):
        """Generated plan has GENERATED status."""
        from baldur.services.settings_recommendation.pipeline import (
            RecommendationPipeline,
        )

        svc = _make_service()
        svc._pipeline = MagicMock(spec=RecommendationPipeline)
        svc._pipeline.run.return_value = ([_make_item()], {})

        plan = svc.generate_recommendations(metrics={"error_rate": 0.05})

        assert plan.status == RecommendationStatus.GENERATED
        assert plan.parameter_count == 1
        assert plan.plan_id  # UUID generated

    def test_generates_empty_plan_when_no_items(self):
        """Empty pipeline output produces plan with 0 items."""
        from baldur.services.settings_recommendation.pipeline import (
            RecommendationPipeline,
        )

        svc = _make_service()
        svc._pipeline = MagicMock(spec=RecommendationPipeline)
        svc._pipeline.run.return_value = ([], {})

        plan = svc.generate_recommendations(metrics={"error_rate": 0.01})

        assert plan.parameter_count == 0
        assert plan.overall_confidence == 0.0

    def test_plan_stored_in_plan_store(self):
        """Generated plan is saved to PlanStore."""
        from baldur.services.settings_recommendation.pipeline import (
            RecommendationPipeline,
        )

        svc = _make_service()
        svc._pipeline = MagicMock(spec=RecommendationPipeline)
        svc._pipeline.run.return_value = ([_make_item()], {})

        plan = svc.generate_recommendations(metrics={"error_rate": 0.05})

        assert svc.get_plan(plan.plan_id) is plan


class TestValidatePlanBehavior:
    """validate_plan status transitions."""

    def test_empty_plan_is_rejected(self):
        """Plan with no items is immediately REJECTED."""
        svc = _make_service()
        plan = RecommendationPlan(plan_id="test", items=[])

        result = svc.validate_plan(plan)

        assert result.status == RecommendationStatus.REJECTED

    def test_no_constraint_engine_validates_immediately(self):
        """Without ConstraintEngine and shadow, plan is VALIDATED."""
        svc = _make_service(constraint_engine=None)
        plan = RecommendationPlan(plan_id="test", items=[_make_item()])

        result = svc.validate_plan(plan)

        assert result.status == RecommendationStatus.VALIDATED

    def test_constraint_failure_rejects_plan(self):
        """ConstraintEngine failure → REJECTED status."""
        from baldur.core.constraint_engine import (
            ConstraintEngine,
            ConstraintResult,
        )

        mock_ce = MagicMock(spec=ConstraintEngine)
        mock_result = MagicMock(spec=ConstraintResult)
        mock_result.passed = False
        mock_result.violations = [MagicMock()]
        mock_ce.validate.return_value = mock_result

        svc = _make_service(constraint_engine=mock_ce)
        plan = RecommendationPlan(plan_id="test", items=[_make_item()])

        result = svc.validate_plan(plan)

        assert result.status == RecommendationStatus.REJECTED
        assert result.constraint_result is mock_result

    def test_shadow_required_sets_validating_status(self):
        """With shadow_required=True, plan transitions to VALIDATING."""
        from baldur.services.config_shadow.service import ShadowEvaluatorService

        mock_shadow = MagicMock(spec=ShadowEvaluatorService)
        mock_eval = MagicMock()
        mock_eval.evaluation_id = "eval-123"
        mock_shadow.submit_evaluation.return_value = mock_eval

        svc = _make_service(shadow_service=mock_shadow)
        svc._settings.shadow_required = True
        plan = RecommendationPlan(plan_id="test", items=[_make_item()])

        result = svc.validate_plan(plan)

        assert result.status == RecommendationStatus.VALIDATING
        assert result.shadow_evaluation_id == "eval-123"


class TestApplyPlanBehavior:
    """apply_plan status transitions and governance."""

    def test_non_validated_plan_not_applied(self):
        """Plan not in VALIDATED status is returned unchanged."""
        svc = _make_service()
        plan = RecommendationPlan(plan_id="test", items=[_make_item()])
        plan.status = RecommendationStatus.GENERATED

        result = svc.apply_plan(plan)

        assert result.status == RecommendationStatus.GENERATED

    def test_validated_plan_applied_directly(self):
        """VALIDATED plan without canary is applied directly."""
        svc = _make_service()
        plan = RecommendationPlan(plan_id="test", items=[_make_item()])
        plan.status = RecommendationStatus.VALIDATED

        result = svc.apply_plan(plan)

        assert result.status == RecommendationStatus.APPLIED
        assert result.applied_at is not None


class TestRunFullPipelineBehavior:
    """run_full_pipeline 2-phase behavior."""

    def test_paused_service_returns_none(self):
        """Paused service returns None without processing."""
        svc = _make_service()
        svc.pause("test")

        assert svc.run_full_pipeline() is None

    def test_generates_and_validates_plan(self):
        """Full pipeline generates then validates plan."""
        from baldur.services.settings_recommendation.pipeline import (
            RecommendationPipeline,
        )

        svc = _make_service()
        svc._pipeline = MagicMock(spec=RecommendationPipeline)
        svc._pipeline.run.return_value = ([_make_item()], {})

        plan = svc.run_full_pipeline(metrics={"error_rate": 0.05})

        assert plan is not None
        assert plan.status == RecommendationStatus.VALIDATED


class TestFindActivePlanBehavior:
    """_find_active_plan matching behavior."""

    def test_finds_applied_plan_matching_params(self):
        """Finds most recent APPLIED plan matching rolled back params."""
        svc = _make_service()
        plan = RecommendationPlan(
            plan_id="test",
            items=[_make_item(parameter="timeout_ms")],
        )
        plan.status = RecommendationStatus.APPLIED
        svc._plan_store.save(plan, 7200)

        result = svc._find_active_plan(["timeout_ms"])

        assert result is plan

    def test_returns_none_when_no_match(self):
        """Returns None when no plan matches rolled back params."""
        svc = _make_service()
        plan = RecommendationPlan(
            plan_id="test",
            items=[_make_item(parameter="retry_count")],
        )
        plan.status = RecommendationStatus.APPLIED
        svc._plan_store.save(plan, 7200)

        result = svc._find_active_plan(["timeout_ms"])

        assert result is None

    def test_ignores_generated_status_plans(self):
        """GENERATED plans are not candidates for rollback matching."""
        svc = _make_service()
        plan = RecommendationPlan(
            plan_id="test",
            items=[_make_item(parameter="timeout_ms")],
        )
        plan.status = RecommendationStatus.GENERATED
        svc._plan_store.save(plan, 7200)

        result = svc._find_active_plan(["timeout_ms"])

        assert result is None


class TestSingletonBehavior:
    """Singleton get/reset behavior."""

    def test_get_returns_same_instance(self):
        """get_settings_recommendation_service returns singleton."""
        reset_settings_recommendation_service()
        svc1 = get_settings_recommendation_service()
        svc2 = get_settings_recommendation_service()
        assert svc1 is svc2
        reset_settings_recommendation_service()

    def test_reset_clears_singleton(self):
        """reset_settings_recommendation_service clears cached instance."""
        svc1 = get_settings_recommendation_service()
        reset_settings_recommendation_service()
        svc2 = get_settings_recommendation_service()
        assert svc1 is not svc2
        reset_settings_recommendation_service()


class TestServiceStatusBehavior:
    """get_status / pause / resume behavior."""

    def test_pause_and_resume_toggle_state(self):
        """pause sets paused=True, resume resets."""
        svc = _make_service()
        svc.pause("maintenance")
        status = svc.get_status()
        assert status["paused"] is True
        assert status["pause_reason"] == "maintenance"

        svc.resume()
        status = svc.get_status()
        assert status["paused"] is False
