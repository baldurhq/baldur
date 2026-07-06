"""Shared fixtures for settings_recommendation unit tests."""

from __future__ import annotations

from baldur.core.decision_engine import AdjustmentPriority
from baldur.services.settings_recommendation.models import (
    RecommendationItem,
    RecommendationPlan,
    RecommendationSource,
)


def _make_item(**overrides) -> RecommendationItem:
    """Create a RecommendationItem with sensible defaults."""
    defaults = {
        "parameter": "timeout_ms",
        "current_value": 5000.0,
        "recommended_value": 6000.0,
        "source": RecommendationSource.RULE_BASED,
        "confidence": 0.85,
        "expected_improvement": 0.2,
        "reason": "test reason",
        "priority": AdjustmentPriority.MEDIUM,
    }
    defaults.update(overrides)
    return RecommendationItem(**defaults)


def _make_plan(**overrides) -> RecommendationPlan:
    """Create a RecommendationPlan with sensible defaults."""
    defaults = {
        "plan_id": "test-plan-001",
        "items": [make_item()],
    }
    defaults.update(overrides)
    return RecommendationPlan(**defaults)


# Aliases for public import
make_item = _make_item
make_plan = _make_plan
