"""
Unit tests for LearningSettings.

Verification targets:
- Design contract values (defaults, field count)
- Environment variable overrides (flat and nested)
- Boundary analysis (ge, gt, le constraints)
- Blacklist TTL None acceptance and zero rejection
- ThrottleSLARule nested model defaults
- Singleton caching / reset

Test subject: baldur.settings.learning
"""

import os
from unittest import mock

import pytest
from pydantic import ValidationError


class TestLearningSettingsContract:
    """LearningSettings design contract verification.

    Validates default values specified in 338_SETTINGS_GAP_EMERGENCY_SAGA_LEARNING.md section 7.
    """

    def test_field_count(self):
        """LearningSettings has exactly 9 fields."""
        with mock.patch.dict(os.environ, {}, clear=True):
            from baldur.settings.learning import (
                LearningSettings,
                reset_learning_settings,
            )

            reset_learning_settings()
            assert len(LearningSettings.model_fields) == 9

    def test_suggestion_threshold_default_is_0_8(self):
        """Default suggestion_threshold is 0.8."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.suggestion_threshold == 0.8

    def test_pattern_min_occurrences_default_is_3(self):
        """Default pattern_min_occurrences is 3."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.pattern_min_occurrences == 3

    def test_anomaly_multiplier_default_is_2_0(self):
        """Default anomaly_multiplier is 2.0."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.anomaly_multiplier == 2.0

    def test_anomaly_window_size_default_is_100(self):
        """Default anomaly_window_size is 100."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.anomaly_window_size == 100

    def test_blacklist_default_ttl_hours_default_is_168(self):
        """Default blacklist_default_ttl_hours is 168 (7 days)."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.blacklist_default_ttl_hours == 168

    def test_max_adjustment_records_default_is_1000(self):
        """Default max_adjustment_records is 1000."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.max_adjustment_records == 1000

    def test_env_prefix_is_baldur_learning(self):
        """env_prefix must be 'BALDUR_LEARNING_'."""
        from baldur.settings.learning import LearningSettings

        assert LearningSettings.model_config["env_prefix"] == "BALDUR_LEARNING_"

    def test_env_nested_delimiter_is_double_underscore(self):
        """env_nested_delimiter must be '__' for nested ThrottleSLARule overrides."""
        from baldur.settings.learning import LearningSettings

        assert LearningSettings.model_config["env_nested_delimiter"] == "__"


class TestThrottleSLARuleContract:
    """ThrottleSLARule nested model default value verification.

    Validates defaults from 338 doc section 7.2.
    """

    def test_sla_warning_up_trigger_ratio_default(self):
        """sla_warning_up.trigger_ratio default is 0.9."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_warning_up.trigger_ratio == 0.9

    def test_sla_warning_up_adjust_multiplier_default(self):
        """sla_warning_up.adjust_multiplier default is 1.15."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_warning_up.adjust_multiplier == 1.15

    def test_sla_warning_up_limit_bound_default(self):
        """sla_warning_up.limit_bound default is 2000."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_warning_up.limit_bound == 2000

    def test_sla_warning_up_min_confidence_default(self):
        """sla_warning_up.min_confidence default is 0.6."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_warning_up.min_confidence == 0.6

    def test_sla_warning_down_trigger_ratio_default(self):
        """sla_warning_down.trigger_ratio default is 0.5."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_warning_down.trigger_ratio == 0.5

    def test_sla_warning_down_adjust_multiplier_default(self):
        """sla_warning_down.adjust_multiplier default is 0.85."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_warning_down.adjust_multiplier == 0.85

    def test_sla_warning_down_limit_bound_default(self):
        """sla_warning_down.limit_bound default is 50."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_warning_down.limit_bound == 50

    def test_sla_warning_down_min_confidence_default(self):
        """sla_warning_down.min_confidence default is 0.6."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_warning_down.min_confidence == 0.6

    def test_sla_critical_up_trigger_ratio_default(self):
        """sla_critical_up.trigger_ratio default is 0.85."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_critical_up.trigger_ratio == 0.85

    def test_sla_critical_up_adjust_multiplier_default(self):
        """sla_critical_up.adjust_multiplier default is 1.15."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_critical_up.adjust_multiplier == 1.15

    def test_sla_critical_up_limit_bound_default(self):
        """sla_critical_up.limit_bound default is 5000."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_critical_up.limit_bound == 5000

    def test_sla_critical_up_min_confidence_default(self):
        """sla_critical_up.min_confidence default is 0.7."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings()
            assert settings.sla_critical_up.min_confidence == 0.7


class TestLearningSettingsBehavior:
    """LearningSettings behavior verification."""

    # === Environment Variable Override ===

    def test_env_override_suggestion_threshold(self):
        """BALDUR_LEARNING_SUGGESTION_THRESHOLD=0.9 overrides default."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_LEARNING_SUGGESTION_THRESHOLD": "0.9"},
            clear=True,
        ):
            settings = LearningSettings()
            assert settings.suggestion_threshold == 0.9

    def test_env_override_nested_sla_warning_up_trigger_ratio(self):
        """Nested env var BALDUR_LEARNING_SLA_WARNING_UP__TRIGGER_RATIO=0.95 overrides default."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_LEARNING_SLA_WARNING_UP__TRIGGER_RATIO": "0.95",
                "BALDUR_LEARNING_SLA_WARNING_UP__ADJUST_MULTIPLIER": "1.15",
                "BALDUR_LEARNING_SLA_WARNING_UP__LIMIT_BOUND": "2000",
            },
            clear=True,
        ):
            settings = LearningSettings()
            assert settings.sla_warning_up.trigger_ratio == 0.95

    # === Blacklist TTL special cases ===

    def test_blacklist_ttl_none_accepted(self):
        """blacklist_default_ttl_hours accepts None (means indefinite)."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = LearningSettings(blacklist_default_ttl_hours=None)
            assert settings.blacklist_default_ttl_hours is None

    def test_blacklist_ttl_zero_raises_validation_error(self):
        """blacklist_default_ttl_hours=0 raises ValidationError (ge=1 constraint)."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValidationError):
                LearningSettings(blacklist_default_ttl_hours=0)

    # === Boundary Analysis ===

    def test_suggestion_threshold_above_maximum_raises(self):
        """suggestion_threshold > 1.0 raises ValidationError (le=1.0 constraint)."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValidationError):
                LearningSettings(suggestion_threshold=1.1)

    def test_anomaly_multiplier_at_minimum_raises(self):
        """anomaly_multiplier=1.0 raises ValidationError (gt=1.0, strictly greater than)."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValidationError):
                LearningSettings(anomaly_multiplier=1.0)

    def test_pattern_min_occurrences_below_minimum_raises(self):
        """pattern_min_occurrences=0 raises ValidationError (ge=1 constraint)."""
        from baldur.settings.learning import (
            LearningSettings,
            reset_learning_settings,
        )

        reset_learning_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValidationError):
                LearningSettings(pattern_min_occurrences=0)

    # === Singleton ===

    def test_singleton_get_returns_via_root_config(self):
        """get_learning_settings() returns the same cached instance on repeated calls."""
        from baldur.settings.learning import (
            get_learning_settings,
            reset_learning_settings,
        )

        reset_learning_settings()
        first = get_learning_settings()
        second = get_learning_settings()
        assert first is second

    def test_singleton_reset_clears_cached_instance(self):
        """reset_learning_settings() clears cache so next get returns a new instance."""
        from baldur.settings.learning import (
            get_learning_settings,
            reset_learning_settings,
        )

        reset_learning_settings()
        first = get_learning_settings()
        reset_learning_settings()
        second = get_learning_settings()
        assert first is not second
