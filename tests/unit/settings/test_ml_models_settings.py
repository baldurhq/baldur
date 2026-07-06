"""
Tests for MLModelsSettings — ML model strategy settings.

Test classification (UNIT_TEST_GUIDELINES §0):
- Contract: default values from design doc §5.2, env var prefix
- Behavior: Pydantic boundary validation, singleton get/reset

Source: settings/ml_models.py
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.ml_models import (
    ARIMAConfig,
    BayesianOptimizerConfig,
    GradientBoostConfig,
    IsolationForestConfig,
    MLModelsSettings,
)

# =============================================================================
# Contract — Default values from design doc §5.2
# =============================================================================


class TestMLModelsSettingsContract:
    """Verify default values match design doc §5.2."""

    def test_enabled_default_false(self):
        """Global ML feature toggle defaults to False."""
        s = MLModelsSettings()
        assert s.enabled is False

    def test_env_prefix_is_baldur_ml(self):
        """Env var prefix = BALDUR_ML_MODELS_ (design doc §5.2)."""
        assert MLModelsSettings.model_config["env_prefix"] == "BALDUR_ML_MODELS_"

    def test_isolation_forest_defaults(self):
        """IsolationForestConfig defaults — Dormant tier per V1_LAUNCH_MANIFEST."""
        c = IsolationForestConfig()
        assert c.enabled is False
        assert c.contamination == 0.05
        assert c.n_estimators == 100
        assert c.min_data_points == 200
        assert c.max_samples == 256
        assert c.max_buffer_size == 2560
        assert c.refit_threshold == 500

    def test_arima_defaults(self):
        """ARIMAConfig defaults — Dormant tier per V1_LAUNCH_MANIFEST."""
        c = ARIMAConfig()
        assert c.enabled is False
        assert c.order == (2, 1, 2)
        assert c.auto_order is True
        assert c.min_data_points == 50
        assert c.max_history == 5000
        assert c.refit_interval == 100

    def test_gradient_boost_defaults(self):
        """GradientBoostConfig defaults — Dormant tier per V1_LAUNCH_MANIFEST."""
        c = GradientBoostConfig()
        assert c.enabled is False
        assert c.n_estimators == 100
        assert c.max_depth == 6
        assert c.learning_rate == 0.1
        assert c.min_data_points == 200
        assert c.prefer_xgboost is True
        assert c.retrain_threshold == 100

    def test_bayesian_defaults(self):
        """BayesianOptimizerConfig defaults — Dormant tier per V1_LAUNCH_MANIFEST."""
        c = BayesianOptimizerConfig()
        assert c.enabled is False
        assert c.kernel == "matern"
        assert c.acquisition == "expected_improvement"
        assert c.exploration_weight == 0.1
        assert c.min_data_points == 20
        assert c.max_observations == 500


# =============================================================================
# Behavior — Pydantic boundary validation
# =============================================================================


class TestMLModelsSettingsBoundaryBehavior:
    """Verify Pydantic field constraints reject out-of-range values."""

    def test_contamination_below_min_rejected(self):
        """contamination < 0.01 raises ValidationError."""
        with pytest.raises(ValidationError):
            IsolationForestConfig(contamination=0.009)

    def test_contamination_above_max_rejected(self):
        """contamination > 0.5 raises ValidationError."""
        with pytest.raises(ValidationError):
            IsolationForestConfig(contamination=0.51)

    def test_n_estimators_below_min_rejected(self):
        """n_estimators < 10 raises ValidationError."""
        with pytest.raises(ValidationError):
            IsolationForestConfig(n_estimators=9)

    def test_exploration_weight_below_min_rejected(self):
        """exploration_weight < 0.0 raises ValidationError."""
        with pytest.raises(ValidationError):
            BayesianOptimizerConfig(exploration_weight=-0.1)

    def test_exploration_weight_above_max_rejected(self):
        """exploration_weight > 1.0 raises ValidationError."""
        with pytest.raises(ValidationError):
            BayesianOptimizerConfig(exploration_weight=1.1)

    def test_max_depth_below_min_rejected(self):
        """max_depth < 2 raises ValidationError."""
        with pytest.raises(ValidationError):
            GradientBoostConfig(max_depth=1)

    def test_max_depth_above_max_rejected(self):
        """max_depth > 15 raises ValidationError."""
        with pytest.raises(ValidationError):
            GradientBoostConfig(max_depth=16)

    def test_learning_rate_below_min_rejected(self):
        """learning_rate < 0.001 raises ValidationError."""
        with pytest.raises(ValidationError):
            GradientBoostConfig(learning_rate=0.0005)

    def test_arima_min_data_points_below_min_rejected(self):
        """ARIMA min_data_points < 20 raises ValidationError."""
        with pytest.raises(ValidationError):
            ARIMAConfig(min_data_points=19)

    def test_bayesian_min_data_points_at_boundary_accepted(self):
        """Bayesian min_data_points = 5 (lower bound) accepted."""
        c = BayesianOptimizerConfig(min_data_points=5)
        assert c.min_data_points == 5

    def test_contamination_at_boundary_accepted(self):
        """contamination = 0.01 (lower bound) accepted."""
        c = IsolationForestConfig(contamination=0.01)
        assert c.contamination == 0.01


# =============================================================================
# Behavior — Singleton get/reset
# =============================================================================


class TestMLModelsSettingsSingletonBehavior:
    """Verify singleton get/reset lifecycle."""

    def test_get_ml_models_settings_returns_instance(self):
        """get_ml_models_settings() returns MLModelsSettings instance."""
        from baldur.settings.ml_models import get_ml_models_settings

        s = get_ml_models_settings()
        assert isinstance(s, MLModelsSettings)

    def test_get_returns_same_instance(self):
        """get_ml_models_settings() returns cached instance (singleton)."""
        from baldur.settings.ml_models import get_ml_models_settings

        s1 = get_ml_models_settings()
        s2 = get_ml_models_settings()
        assert s1 is s2

    def test_reset_allows_new_instance(self):
        """reset_ml_models_settings() clears cache so next get creates new instance."""
        from baldur.settings.ml_models import (
            get_ml_models_settings,
            reset_ml_models_settings,
        )

        s1 = get_ml_models_settings()
        reset_ml_models_settings()
        s2 = get_ml_models_settings()
        assert s1 is not s2
