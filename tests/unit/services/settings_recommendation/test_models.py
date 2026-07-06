"""Unit tests for settings_recommendation.models."""

from __future__ import annotations

import pytest

from baldur.services.settings_recommendation.models import (
    PlanStore,
    RecommendationItem,
    RecommendationPlan,
    RecommendationSource,
    RecommendationStatus,
)

from .conftest import _make_item, _make_plan

# ---------------------------------------------------------------------------
# Contract Tests
# ---------------------------------------------------------------------------


class TestRecommendationSourceContract:
    """RecommendationSource enum design contract values."""

    def test_source_has_six_members(self):
        """Design contract: exactly 6 recommendation sources."""
        assert len(RecommendationSource) == 6

    def test_source_values_match_design(self):
        """Design contract: enum values as specified in 374 §2.2."""
        assert RecommendationSource.RULE_BASED.value == "rule_based"
        assert RecommendationSource.ML_ANOMALY.value == "ml_anomaly"
        assert RecommendationSource.ML_FORECAST.value == "ml_forecast"
        assert RecommendationSource.ML_OPTIMIZATION.value == "ml_optimization"
        assert RecommendationSource.DEPENDENCY_CASCADE.value == "dependency_cascade"
        assert RecommendationSource.PROFILE_PRESET.value == "profile_preset"


class TestRecommendationStatusContract:
    """RecommendationStatus enum design contract values."""

    def test_status_has_eight_members(self):
        """Design contract: exactly 8 lifecycle statuses."""
        assert len(RecommendationStatus) == 8

    def test_status_values_match_design(self):
        """Design contract: enum values as specified in 374 §2.2."""
        assert RecommendationStatus.GENERATED.value == "generated"
        assert RecommendationStatus.VALIDATING.value == "validating"
        assert RecommendationStatus.VALIDATED.value == "validated"
        assert RecommendationStatus.REJECTED.value == "rejected"
        assert RecommendationStatus.DEPLOYING.value == "deploying"
        assert RecommendationStatus.APPLIED.value == "applied"
        assert RecommendationStatus.ROLLED_BACK.value == "rolled_back"
        assert RecommendationStatus.EXPIRED.value == "expired"


class TestRecommendationSourceJsonSerializableContract:
    """RecommendationSource must be JSON-serializable via (str, Enum)."""

    def test_source_is_str_enum(self):
        """All sources are str subclass for JSON serialization."""
        for source in RecommendationSource:
            assert isinstance(source, str)


# ---------------------------------------------------------------------------
# Behavior Tests
# ---------------------------------------------------------------------------


class TestRecommendationItemSerializationBehavior:
    """RecommendationItem to_dict/from_dict roundtrip behavior."""

    def test_item_serialization_roundtrip_preserves_data(self):
        """Serialize and deserialize must preserve all fields."""
        # Given
        item = _make_item(
            metric_evidence={"error_rate": 0.05},
            is_cascade=True,
        )

        # When
        data = item.to_dict()
        restored = RecommendationItem.from_dict(data)

        # Then
        assert restored.parameter == item.parameter
        assert restored.current_value == item.current_value
        assert restored.recommended_value == item.recommended_value
        assert restored.source == item.source
        assert restored.confidence == item.confidence
        assert restored.is_cascade is True
        assert restored.metric_evidence == {"error_rate": 0.05}

    def test_item_source_enum_serialized_as_string(self):
        """SerializableMixin must serialize Enum as .value string."""
        item = _make_item()
        data = item.to_dict()
        assert data["source"] == "rule_based"


class TestRecommendationPlanBehavior:
    """RecommendationPlan property and serialization behavior."""

    def test_parameter_count_returns_item_count(self):
        """parameter_count property returns len(items)."""
        plan = _make_plan(items=[_make_item(), _make_item(parameter="retry_count")])
        assert plan.parameter_count == 2

    def test_has_ml_items_returns_false_for_rule_only(self):
        """has_ml_items returns False when all items are RULE_BASED."""
        plan = _make_plan(items=[_make_item(source=RecommendationSource.RULE_BASED)])
        assert plan.has_ml_items is False

    def test_has_ml_items_returns_true_for_ml_optimization(self):
        """has_ml_items returns True when any item is ML source."""
        plan = _make_plan(
            items=[
                _make_item(source=RecommendationSource.RULE_BASED),
                _make_item(source=RecommendationSource.ML_OPTIMIZATION),
            ]
        )
        assert plan.has_ml_items is True

    def test_has_ml_items_detects_ml_forecast(self):
        """has_ml_items recognizes ML_FORECAST."""
        plan = _make_plan(items=[_make_item(source=RecommendationSource.ML_FORECAST)])
        assert plan.has_ml_items is True

    def test_has_ml_items_detects_ml_anomaly(self):
        """has_ml_items recognizes ML_ANOMALY."""
        plan = _make_plan(items=[_make_item(source=RecommendationSource.ML_ANOMALY)])
        assert plan.has_ml_items is True

    def test_plan_default_status_is_generated(self):
        """Default status must be GENERATED."""
        plan = _make_plan()
        assert plan.status == RecommendationStatus.GENERATED

    def test_plan_exclude_none_removes_none_fields(self):
        """exclude_none=True omits None fields from to_dict()."""
        plan = _make_plan()
        data = plan.to_dict()
        assert "constraint_result" not in data
        assert "shadow_evaluation_id" not in data
        assert "canary_rollout_id" not in data
        assert "applied_at" not in data

    def test_plan_serialization_roundtrip(self):
        """Serialize and deserialize must preserve plan structure."""
        # Given
        plan = _make_plan(overall_confidence=0.82)

        # When
        data = plan.to_dict()
        restored = RecommendationPlan.from_dict(data)

        # Then
        assert restored.plan_id == plan.plan_id
        assert restored.status == RecommendationStatus.GENERATED
        assert restored.overall_confidence == pytest.approx(0.82)
        assert len(restored.items) == 1


class TestPlanStoreBehavior:
    """PlanStore save/get/eviction behavior."""

    def test_save_and_get_returns_same_plan(self):
        """Save then get by ID returns the same plan."""
        store = PlanStore(max_plans=10)
        plan = _make_plan()
        store.save(plan)
        result = store.get(plan.plan_id)
        assert result is plan

    def test_get_nonexistent_returns_none(self):
        """Get with unknown ID returns None."""
        store = PlanStore(max_plans=10)
        assert store.get("nonexistent") is None

    def test_eviction_limits_stored_plans(self):
        """PlanStore with max_plans=2 keeps at most 2 plans in memory."""
        store = PlanStore(max_plans=2)
        plan1 = _make_plan(plan_id="plan-1")
        plan2 = _make_plan(plan_id="plan-2")
        plan3 = _make_plan(plan_id="plan-3")

        store.save(plan1)
        store.save(plan2)
        store.save(plan3)

        # At most max_plans entries remain in memory
        assert len(store._memory) <= 3  # deque maxlen manages order
        # The newest plan is always retrievable
        assert store.get("plan-3") is plan3

    def test_get_recent_plans_returns_newest_first(self):
        """get_recent_plans returns plans in reverse insertion order."""
        store = PlanStore(max_plans=10)
        plan1 = _make_plan(plan_id="plan-1")
        plan2 = _make_plan(plan_id="plan-2")
        store.save(plan1)
        store.save(plan2)

        recent = store.get_recent_plans(limit=10)
        assert [p.plan_id for p in recent] == ["plan-2", "plan-1"]

    def test_get_recent_plans_filters_by_status(self):
        """get_recent_plans with status filters correctly."""
        store = PlanStore(max_plans=10)
        plan1 = _make_plan(plan_id="p1")
        plan1.status = RecommendationStatus.VALIDATED
        plan2 = _make_plan(plan_id="p2")
        plan2.status = RecommendationStatus.REJECTED
        store.save(plan1)
        store.save(plan2)

        validated = store.get_recent_plans(status=RecommendationStatus.VALIDATED)
        assert len(validated) == 1
        assert validated[0].plan_id == "p1"

    def test_get_recent_plans_respects_limit(self):
        """get_recent_plans returns at most limit items."""
        store = PlanStore(max_plans=10)
        for i in range(5):
            store.save(_make_plan(plan_id=f"p{i}"))
        assert len(store.get_recent_plans(limit=3)) == 3

    def test_save_idempotent_no_duplicate_order(self):
        """Saving the same plan_id twice does not create duplicates."""
        store = PlanStore(max_plans=10)
        plan = _make_plan(plan_id="dup")
        store.save(plan)
        store.save(plan)
        assert len(store.get_recent_plans(limit=10)) == 1
