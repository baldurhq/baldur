"""Unit tests for settings_recommendation.presets."""

from __future__ import annotations

from baldur.services.settings_recommendation.presets import (
    WORKLOAD_PRESETS,
    WorkloadProfile,
    apply_workload_profile,
)

# ---------------------------------------------------------------------------
# Contract Tests
# ---------------------------------------------------------------------------


class TestWorkloadProfileContract:
    """WorkloadProfile enum design contract values."""

    def test_profile_has_six_members(self):
        """Design contract: exactly 6 workload profiles."""
        assert len(WorkloadProfile) == 6

    def test_profile_values_match_design(self):
        """Design contract: enum values as specified in 374 §3.2."""
        assert WorkloadProfile.API_GATEWAY.value == "api_gateway"
        assert WorkloadProfile.BATCH_PROCESSOR.value == "batch_processor"
        assert WorkloadProfile.EVENT_DRIVEN.value == "event_driven"
        assert WorkloadProfile.MICROSERVICE.value == "microservice"
        assert WorkloadProfile.DATA_PIPELINE.value == "data_pipeline"
        assert WorkloadProfile.REAL_TIME.value == "real_time"


class TestWorkloadPresetsContract:
    """WORKLOAD_PRESETS dictionary design contract values."""

    def test_all_profiles_have_presets(self):
        """Every WorkloadProfile must have a corresponding preset."""
        for profile in WorkloadProfile:
            assert profile in WORKLOAD_PRESETS, f"Missing preset for {profile}"

    def test_api_gateway_preset_values(self):
        """API_GATEWAY preset contract values from 374 §3.3."""
        preset = WORKLOAD_PRESETS[WorkloadProfile.API_GATEWAY]
        assert preset["circuit_breaker_threshold"] == 0.3
        assert preset["retry_count"] == 2
        assert preset["timeout_ms"] == 3000
        assert preset["rate_limit_rps"] == 5000
        assert preset["connection_pool_size"] == 50

    def test_batch_processor_preset_values(self):
        """BATCH_PROCESSOR preset contract values from 374 §3.3."""
        preset = WORKLOAD_PRESETS[WorkloadProfile.BATCH_PROCESSOR]
        assert preset["circuit_breaker_threshold"] == 0.7
        assert preset["retry_count"] == 5
        assert preset["timeout_ms"] == 30000
        assert preset["connection_pool_size"] == 10

    def test_real_time_preset_values(self):
        """REAL_TIME preset contract values from 374 §3.3."""
        preset = WORKLOAD_PRESETS[WorkloadProfile.REAL_TIME]
        assert preset["circuit_breaker_threshold"] == 0.2
        assert preset["retry_count"] == 1
        assert preset["timeout_ms"] == 1000
        assert preset["rate_limit_rps"] == 10000
        assert preset["connection_pool_size"] == 80

    def test_all_presets_have_required_keys(self):
        """All presets must include the 10 standard tunable parameters."""
        required_keys = {
            "circuit_breaker_threshold",
            "circuit_breaker_recovery_timeout",
            "retry_count",
            "backoff_base_ms",
            "backoff_max_ms",
            "timeout_ms",
            "rate_limit_rps",
            "jitter_range",
            "throttle_sla_warning_ms",
            "throttle_sla_critical_ms",
            "connection_pool_size",
        }
        for profile, preset in WORKLOAD_PRESETS.items():
            missing = required_keys - set(preset.keys())
            assert not missing, f"{profile}: missing keys {missing}"


# ---------------------------------------------------------------------------
# Behavior Tests
# ---------------------------------------------------------------------------


class TestApplyWorkloadProfileBehavior:
    """apply_workload_profile merge order behavior."""

    def test_workload_profile_applied_without_scale(self):
        """Workload profile settings are returned when no scale profile."""
        result = apply_workload_profile(WorkloadProfile.MICROSERVICE)
        expected = WORKLOAD_PRESETS[WorkloadProfile.MICROSERVICE]
        for key, value in expected.items():
            assert result[key] == value

    def test_custom_overrides_win_over_workload(self):
        """Custom overrides take precedence over workload preset."""
        result = apply_workload_profile(
            WorkloadProfile.API_GATEWAY,
            custom_overrides={"timeout_ms": 9999},
        )
        assert result["timeout_ms"] == 9999
        # Other values preserved from workload preset
        assert (
            result["retry_count"]
            == WORKLOAD_PRESETS[WorkloadProfile.API_GATEWAY]["retry_count"]
        )

    def test_custom_overrides_can_add_new_keys(self):
        """Custom overrides can introduce keys not in presets."""
        result = apply_workload_profile(
            WorkloadProfile.MICROSERVICE,
            custom_overrides={"custom_key": "custom_value"},
        )
        assert result["custom_key"] == "custom_value"

    def test_no_overrides_returns_preset_only(self):
        """Without overrides, result matches the workload preset exactly."""
        result = apply_workload_profile(WorkloadProfile.DATA_PIPELINE)
        preset = WORKLOAD_PRESETS[WorkloadProfile.DATA_PIPELINE]
        assert result == preset
