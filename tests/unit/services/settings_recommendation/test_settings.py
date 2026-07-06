"""Unit tests for settings/settings_recommendation.py."""

from __future__ import annotations

import pytest

from baldur.services.settings_recommendation.models import CanaryStageConfig
from baldur.settings.settings_recommendation import (
    SettingsRecommendationSettings,
)

# ---------------------------------------------------------------------------
# Contract Tests
# ---------------------------------------------------------------------------


class TestSettingsRecommendationContract:
    """Design contract values from 374 §5.1."""

    def test_default_enabled_is_false(self):
        """Feature disabled by default."""
        s = SettingsRecommendationSettings()
        assert s.enabled is False

    def test_default_mode_is_rule_based(self):
        """Default mode is rule_based."""
        s = SettingsRecommendationSettings()
        assert s.mode == "rule_based"

    def test_default_auto_apply_is_false(self):
        """Auto-apply disabled by default (safety)."""
        s = SettingsRecommendationSettings()
        assert s.auto_apply is False

    def test_default_min_confidence(self):
        """Min confidence threshold default: 0.7."""
        s = SettingsRecommendationSettings()
        assert s.min_confidence == 0.7

    def test_default_max_changes_per_cycle(self):
        """Max changes per cycle default: 5."""
        s = SettingsRecommendationSettings()
        assert s.max_changes_per_cycle == 5

    def test_default_schedule_seconds(self):
        """Schedule interval default: 3600 (1 hour)."""
        s = SettingsRecommendationSettings()
        assert s.schedule_seconds == 3600

    def test_default_cooldown_seconds(self):
        """Cooldown default: 7200 (2 hours)."""
        s = SettingsRecommendationSettings()
        assert s.cooldown_seconds == 7200

    def test_default_shadow_required_is_true(self):
        """Shadow evaluation required by default."""
        s = SettingsRecommendationSettings()
        assert s.shadow_required is True

    def test_default_canary_required_is_true(self):
        """Canary deployment required by default."""
        s = SettingsRecommendationSettings()
        assert s.canary_required is True

    def test_default_ml_objective_metrics(self):
        """ML objective metrics default: error_rate + p99_latency_ms."""
        s = SettingsRecommendationSettings()
        assert s.ml_objective_metrics == ["error_rate", "p99_latency_ms"]

    def test_default_canary_stages_three_stages(self):
        """Default canary stages: 10% → 50% → 100%."""
        s = SettingsRecommendationSettings()
        assert len(s.canary_stages) == 3
        assert s.canary_stages[0].percentage == 10
        assert s.canary_stages[1].percentage == 50
        assert s.canary_stages[2].percentage == 100

    def test_default_history_grouping_window(self):
        """History grouping window default: 30 seconds."""
        s = SettingsRecommendationSettings()
        assert s.history_grouping_window_seconds == 30

    def test_default_max_plans(self):
        """Max plans default: 200."""
        s = SettingsRecommendationSettings()
        assert s.max_plans == 200


# ---------------------------------------------------------------------------
# Boundary Analysis Tests
# ---------------------------------------------------------------------------


class TestSettingsRecommendationBoundary:
    """Boundary validation for canary_stages @model_validator."""

    def test_canary_stages_empty_raises(self):
        """Empty canary_stages must raise ValueError."""
        with pytest.raises(ValueError, match="at least one stage"):
            SettingsRecommendationSettings(canary_stages=[])

    def test_canary_stages_not_ascending_raises(self):
        """Non-ascending percentages must raise ValueError."""
        with pytest.raises(ValueError, match="ascending order"):
            SettingsRecommendationSettings(
                canary_stages=[
                    CanaryStageConfig(percentage=50, duration_minutes=30),
                    CanaryStageConfig(percentage=10, duration_minutes=60),
                    CanaryStageConfig(percentage=100, duration_minutes=0),
                ]
            )

    def test_canary_stages_last_not_100_raises(self):
        """Last stage must have percentage=100."""
        with pytest.raises(ValueError, match="percentage=100"):
            SettingsRecommendationSettings(
                canary_stages=[
                    CanaryStageConfig(percentage=10, duration_minutes=30),
                    CanaryStageConfig(percentage=50, duration_minutes=0),
                ]
            )

    def test_canary_stages_negative_duration_raises(self):
        """Negative duration must raise ValueError."""
        with pytest.raises(ValueError, match="duration must be >= 0"):
            SettingsRecommendationSettings(
                canary_stages=[
                    CanaryStageConfig(percentage=100, duration_minutes=-1),
                ]
            )

    def test_canary_stages_single_100_valid(self):
        """Single 100% stage is valid (dev environment)."""
        s = SettingsRecommendationSettings(
            canary_stages=[CanaryStageConfig(percentage=100, duration_minutes=0)]
        )
        assert len(s.canary_stages) == 1
        assert s.canary_stages[0].percentage == 100

    def test_mode_invalid_value_raises(self):
        """Invalid mode literal must raise validation error."""
        with pytest.raises(Exception):
            SettingsRecommendationSettings(mode="invalid_mode")

    def test_schedule_seconds_below_minimum_raises(self):
        """schedule_seconds < 60 must raise."""
        with pytest.raises(Exception):
            SettingsRecommendationSettings(schedule_seconds=59)

    def test_schedule_seconds_at_minimum_valid(self):
        """schedule_seconds = 60 is valid (boundary)."""
        s = SettingsRecommendationSettings(schedule_seconds=60)
        assert s.schedule_seconds == 60
