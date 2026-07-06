"""
Unit tests for EmergencyModeSettings.

Verification items:
- Design contract values (defaults, field count, env_prefix)
- Environment variable overrides
- Boundary analysis (ge, le constraints)
- Model validator (score threshold ordering, throttle multiplier ordering, penalty ordering)
- JSON validation (level_rules_json structure, tier keys, value range)
- Singleton caching/reset

Test target: baldur.settings.emergency_mode
"""

import json
import os
from unittest import mock

import pytest
from pydantic import ValidationError


class TestEmergencyModeSettingsContract:
    """EmergencyModeSettings design contract verification.

    Validates default values specified in 338_SETTINGS_GAP_EMERGENCY_SAGA_LEARNING.md §5.
    """

    def test_field_count(self):
        """EmergencyModeSettings has exactly 24 fields."""
        from baldur.settings.emergency_mode import EmergencyModeSettings

        assert len(EmergencyModeSettings.model_fields) == 25

    def test_stabilization_period_seconds_default(self):
        """Default stabilization_period_seconds is 300."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.stabilization_period_seconds == 300

    def test_cpu_threshold_percent_default(self):
        """Default cpu_threshold_percent is 80.0."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.cpu_threshold_percent == 80.0

    def test_error_rate_threshold_default(self):
        """Default error_rate_threshold is 0.05."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.error_rate_threshold == 0.05

    def test_level_step_delay_seconds_default(self):
        """Default level_step_delay_seconds is 60."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.level_step_delay_seconds == 60

    def test_health_check_interval_seconds_default(self):
        """Default health_check_interval_seconds is 30."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.health_check_interval_seconds == 30

    def test_auto_activate_duration_minutes_default(self):
        """Default auto_activate_duration_minutes is 30."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.auto_activate_duration_minutes == 30

    def test_cache_ttl_seconds_default(self):
        """Default cache_ttl_seconds is 30."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.cache_ttl_seconds == 30

    def test_max_snapshots_default(self):
        """Default max_snapshots is 10."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.max_snapshots == 10

    def test_max_history_default(self):
        """Default max_history is 100."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.max_history == 100

    def test_l1_score_threshold_default(self):
        """Default l1_score_threshold is 0.4."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.l1_score_threshold == 0.4

    def test_l1_confidence_threshold_default(self):
        """Default l1_confidence_threshold is 0.5."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.l1_confidence_threshold == 0.5

    def test_l2_score_threshold_default(self):
        """Default l2_score_threshold is 0.6."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.l2_score_threshold == 0.6

    def test_l2_min_services_default(self):
        """Default l2_min_services is 2."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.l2_min_services == 2

    def test_l3_score_threshold_default(self):
        """Default l3_score_threshold is 0.8."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.l3_score_threshold == 0.8

    def test_l3_min_services_default(self):
        """Default l3_min_services is 3."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.l3_min_services == 3

    def test_l3_min_cascade_depth_default(self):
        """Default l3_min_cascade_depth is 3."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.l3_min_cascade_depth == 3

    def test_throttle_l1_multiplier_default(self):
        """Default throttle_l1_multiplier is 0.8."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.throttle_l1_multiplier == 0.8

    def test_throttle_l2_multiplier_default(self):
        """Default throttle_l2_multiplier is 0.5."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.throttle_l2_multiplier == 0.5

    def test_penalty_regional_strict_default(self):
        """Default penalty_regional_strict is 20.0."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.penalty_regional_strict == 20.0

    def test_penalty_global_strict_default(self):
        """Default penalty_global_strict is 30.0."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.penalty_global_strict == 30.0

    def test_penalty_level_1_default(self):
        """Default penalty_level_1 is 5.0."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.penalty_level_1 == 5.0

    def test_penalty_level_2_default(self):
        """Default penalty_level_2 is 10.0."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.penalty_level_2 == 10.0

    def test_level_rules_json_default(self):
        """Default level_rules_json is None."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.level_rules_json is None

    def test_recovery_dampening_multipliers_json_default(self):
        """Default recovery_dampening_multipliers_json is None."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.recovery_dampening_multipliers_json is None

    def test_env_prefix_is_baldur_emergency_mode(self):
        """env_prefix must be 'BALDUR_EMERGENCY_MODE_'."""
        from baldur.settings.emergency_mode import EmergencyModeSettings

        assert (
            EmergencyModeSettings.model_config["env_prefix"] == "BALDUR_EMERGENCY_MODE_"
        )


class TestEmergencyModeSettingsBehavior:
    """EmergencyModeSettings behavior verification."""

    # === Environment Variable Override ===

    def test_env_override_stabilization_period(self):
        """Environment variable overrides stabilization_period_seconds to 600."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_EMERGENCY_MODE_STABILIZATION_PERIOD_SECONDS": "600"},
            clear=True,
        ):
            settings = EmergencyModeSettings()
            assert settings.stabilization_period_seconds == 600

    def test_env_override_l1_score_threshold(self):
        """Environment variable overrides l1_score_threshold."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_EMERGENCY_MODE_L1_SCORE_THRESHOLD": "0.3"},
            clear=True,
        ):
            settings = EmergencyModeSettings()
            assert settings.l1_score_threshold == 0.3

    # === Boundary: Score threshold ordering (model_validator) ===

    def test_score_threshold_ordering_violation_raises(self):
        """L1 >= L2 score threshold raises ValueError."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_EMERGENCY_MODE_L1_SCORE_THRESHOLD": "0.7",
                "BALDUR_EMERGENCY_MODE_L2_SCORE_THRESHOLD": "0.6",
                "BALDUR_EMERGENCY_MODE_L3_SCORE_THRESHOLD": "0.8",
            },
            clear=True,
        ):
            with pytest.raises(
                ValidationError, match="Score thresholds must be ordered"
            ):
                EmergencyModeSettings()

    # === Boundary: Throttle multiplier ordering (model_validator) ===

    def test_throttle_multiplier_ordering_violation_raises(self):
        """throttle_l1_multiplier < throttle_l2_multiplier raises ValueError."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_EMERGENCY_MODE_THROTTLE_L1_MULTIPLIER": "0.3",
                "BALDUR_EMERGENCY_MODE_THROTTLE_L2_MULTIPLIER": "0.5",
            },
            clear=True,
        ):
            with pytest.raises(ValidationError, match="throttle_l1_multiplier"):
                EmergencyModeSettings()

    # === Boundary: Penalty ordering (model_validator) ===

    def test_penalty_ordering_violation_raises(self):
        """penalty_level_1 > penalty_level_2 raises ValueError."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_EMERGENCY_MODE_PENALTY_LEVEL_1": "15.0",
                "BALDUR_EMERGENCY_MODE_PENALTY_LEVEL_2": "10.0",
            },
            clear=True,
        ):
            with pytest.raises(ValidationError, match="penalty_level_1"):
                EmergencyModeSettings()

    # === Boundary: stabilization_period_seconds ge=30 ===

    def test_stabilization_period_below_minimum_raises(self):
        """stabilization_period_seconds=29 raises ValidationError (ge=30)."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_EMERGENCY_MODE_STABILIZATION_PERIOD_SECONDS": "29"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                EmergencyModeSettings()

    # === Boundary: throttle multiplier le=1.0 ===

    def test_multiplier_above_maximum_raises(self):
        """throttle_l1_multiplier > 1.0 raises ValidationError (le=1.0)."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_EMERGENCY_MODE_THROTTLE_L1_MULTIPLIER": "1.1"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                EmergencyModeSettings()

    # === JSON Validation: level_rules_json ===

    def test_level_rules_json_valid_structure_accepted(self):
        """Valid level_rules_json with all required tiers is accepted."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        valid_rules = {
            "1": {"critical": 1.0, "standard": 1.0, "non_essential": 0.0},
            "2": {"critical": 1.0, "standard": 0.1, "non_essential": 0.0},
            "3": {"critical": 0.5, "standard": 0.0, "non_essential": 0.0},
        }
        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_EMERGENCY_MODE_LEVEL_RULES_JSON": json.dumps(valid_rules)},
            clear=True,
        ):
            settings = EmergencyModeSettings()
            assert settings.level_rules_json is not None

    def test_level_rules_json_missing_tier_raises(self):
        """level_rules_json missing a required tier raises ValueError."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        invalid_rules = {
            "1": {"critical": 1.0, "standard": 1.0},
        }
        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_EMERGENCY_MODE_LEVEL_RULES_JSON": json.dumps(invalid_rules)},
            clear=True,
        ):
            with pytest.raises(ValidationError, match="missing required tiers"):
                EmergencyModeSettings()

    def test_level_rules_json_value_out_of_range_raises(self):
        """level_rules_json tier value outside 0.0-1.0 raises ValueError."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        invalid_rules = {
            "1": {"critical": 1.5, "standard": 1.0, "non_essential": 0.0},
        }
        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_EMERGENCY_MODE_LEVEL_RULES_JSON": json.dumps(invalid_rules)},
            clear=True,
        ):
            with pytest.raises(ValidationError, match="must be 0.0-1.0"):
                EmergencyModeSettings()

    def test_level_rules_json_invalid_json_raises(self):
        """level_rules_json with invalid JSON string raises ValueError."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_EMERGENCY_MODE_LEVEL_RULES_JSON": "{not valid json}"},
            clear=True,
        ):
            with pytest.raises(ValidationError, match="not valid JSON"):
                EmergencyModeSettings()

    def test_get_parsed_level_rules_returns_none_when_not_set(self):
        """get_parsed_level_rules() returns None when level_rules_json is None."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.get_parsed_level_rules() is None

    def test_get_parsed_level_rules_returns_dict_when_set(self):
        """get_parsed_level_rules() returns dict with int keys when set."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        valid_rules = {
            "1": {"critical": 1.0, "standard": 1.0, "non_essential": 0.0},
            "2": {"critical": 1.0, "standard": 0.1, "non_essential": 0.0},
        }
        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_EMERGENCY_MODE_LEVEL_RULES_JSON": json.dumps(valid_rules)},
            clear=True,
        ):
            settings = EmergencyModeSettings()
            parsed = settings.get_parsed_level_rules()
            assert isinstance(parsed, dict)
            assert 1 in parsed
            assert 2 in parsed
            assert parsed[1]["critical"] == 1.0

    # === Recovery Dampening Multipliers JSON ===

    def test_recovery_dampening_valid_json_accepted(self):
        """Valid recovery dampening multipliers JSON array is accepted."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_EMERGENCY_MODE_RECOVERY_DAMPENING_MULTIPLIERS_JSON": json.dumps(
                    [0.5, 0.75, 1.0]
                ),
            },
            clear=True,
        ):
            settings = EmergencyModeSettings()
            result = settings.get_parsed_recovery_dampening_multipliers()
            assert result == (0.5, 0.75, 1.0)

    def test_recovery_dampening_not_ascending_raises(self):
        """Non-ascending recovery dampening values raise ValueError."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_EMERGENCY_MODE_RECOVERY_DAMPENING_MULTIPLIERS_JSON": json.dumps(
                    [0.9, 0.8, 1.0]
                ),
            },
            clear=True,
        ):
            with pytest.raises(ValidationError, match="ascending"):
                EmergencyModeSettings()

    def test_recovery_dampening_last_not_1_raises(self):
        """Recovery dampening last value != 1.0 raises ValueError."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_EMERGENCY_MODE_RECOVERY_DAMPENING_MULTIPLIERS_JSON": json.dumps(
                    [0.5, 0.8]
                ),
            },
            clear=True,
        ):
            with pytest.raises(ValidationError, match="last value must be 1.0"):
                EmergencyModeSettings()

    def test_recovery_dampening_value_out_of_range_raises(self):
        """Recovery dampening value > 1.0 raises ValueError."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_EMERGENCY_MODE_RECOVERY_DAMPENING_MULTIPLIERS_JSON": json.dumps(
                    [0.8, 1.5]
                ),
            },
            clear=True,
        ):
            with pytest.raises(ValidationError, match="must be 0.0-1.0"):
                EmergencyModeSettings()

    def test_recovery_dampening_none_returns_none(self):
        """get_parsed_recovery_dampening_multipliers() returns None when not set."""
        from baldur.settings.emergency_mode import (
            EmergencyModeSettings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EmergencyModeSettings()
            assert settings.get_parsed_recovery_dampening_multipliers() is None

    # === Singleton ===

    def test_singleton_get_returns_via_root_config(self):
        """get_emergency_mode_settings() returns the same instance on repeated calls."""
        from baldur.settings.emergency_mode import (
            get_emergency_mode_settings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        first = get_emergency_mode_settings()
        second = get_emergency_mode_settings()
        assert first is second

    def test_singleton_reset_clears_cached_instance(self):
        """reset_emergency_mode_settings() clears the cached instance."""
        from baldur.settings.emergency_mode import (
            get_emergency_mode_settings,
            reset_emergency_mode_settings,
        )

        reset_emergency_mode_settings()
        first = get_emergency_mode_settings()
        reset_emergency_mode_settings()
        second = get_emergency_mode_settings()
        assert first is not second
