"""
Tests for Settings Extensions and New Settings.

확장/신규 생성된 Settings 모듈에 대한 테스트입니다:
- ErrorBudgetPropagationSettings 확장 필드
- ThrottleSettings 확장 필드
- AntiFlappingSettings 확장 필드
- GovernanceSettings 확장 필드
- SamplingSettings (신규)
- SteadyStateSettings (신규)
"""

import os
from unittest import mock

import pytest

# =============================================================================
# ErrorBudgetPropagationSettings Extension Tests
# =============================================================================


class TestErrorBudgetPropagationSettingsExtension:
    """ErrorBudgetPropagationSettings core field tests.

    Ghost fields (propagation_delay_ms, max_crisis_multiplier_cap,
    max_domain_multiplier, default_cache_ttl_seconds, refund_ratio,
    refund_proposal_expiry_hours, default_combine_strategy,
    max_combined_multiplier) were removed — they belonged to other
    settings classes and were never wired to consuming code.
    """

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.error_budget_propagation import (
            reset_error_budget_propagation_settings,
        )

        reset_error_budget_propagation_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.error_budget_propagation import (
            reset_error_budget_propagation_settings,
        )

        reset_error_budget_propagation_settings()

    def test_core_default_values(self):
        """Core propagation field defaults.

        enabled default flipped to False per impl 527 (v1.1 deferred).
        """
        from baldur.settings.error_budget_propagation import (
            get_error_budget_propagation_settings,
        )

        settings = get_error_budget_propagation_settings()

        assert settings.decay_per_hop == 0.5
        assert settings.max_hops == 3
        assert settings.base_multiplier == 5.0
        assert settings.min_multiplier == 1.0
        assert settings.enabled is False

    def test_ghost_fields_removed(self):
        """Verify ghost fields no longer exist."""
        from baldur.settings.error_budget_propagation import (
            ErrorBudgetPropagationSettings,
        )

        settings = ErrorBudgetPropagationSettings()
        removed_fields = [
            "propagation_delay_ms",
            "max_crisis_multiplier_cap",
            "max_domain_multiplier",
            "default_cache_ttl_seconds",
            "refund_ratio",
            "refund_proposal_expiry_hours",
            "default_combine_strategy",
            "max_combined_multiplier",
        ]
        for field_name in removed_fields:
            assert not hasattr(settings, field_name), (
                f"Ghost field '{field_name}' should have been removed"
            )


# =============================================================================
# ThrottleSettings Extension Tests
# =============================================================================


class TestThrottleSettingsExtension:
    """ThrottleSettings 확장 필드 테스트."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.throttle import reset_throttle_settings

        reset_throttle_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.throttle import reset_throttle_settings

        reset_throttle_settings()

    def test_extended_default_values(self):
        """Verify default values for extended fields."""
        from baldur.settings.throttle import get_throttle_settings

        settings = get_throttle_settings()

        # Existing fields
        assert settings.smoothing_factor == 0.5
        assert settings.sample_interval_ms == 500

    def test_extended_env_override(self):
        """Verify env var override for extended fields."""
        from baldur.settings.throttle import (
            get_throttle_settings,
            reset_throttle_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_THROTTLE_SAMPLE_INTERVAL_MS": "1000",
                "BALDUR_THROTTLE_SMOOTHING_FACTOR": "0.7",
            },
        ):
            reset_throttle_settings()
            settings = get_throttle_settings()

            assert settings.sample_interval_ms == 1000
            assert settings.smoothing_factor == 0.7


# =============================================================================
# AntiFlappingSettings Extension Tests
# =============================================================================


class TestAntiFlappingSettingsExtension:
    """AntiFlappingSettings 확장 필드 테스트."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.anti_flapping import reset_anti_flapping_settings

        reset_anti_flapping_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.anti_flapping import reset_anti_flapping_settings

        reset_anti_flapping_settings()

    def test_extended_default_values(self):
        """확장된 필드들의 기본값 검증 (AntiFlappingWindow 기반)."""
        from baldur.settings.anti_flapping import get_anti_flapping_settings

        settings = get_anti_flapping_settings()

        # AntiFlappingWindow 설정
        assert settings.window_seconds == 60
        assert settings.similarity_threshold == 0.01  # 1%
        assert settings.max_similar_changes == 3

    def test_extended_env_override(self):
        """확장된 필드들의 환경 변수 오버라이드 테스트."""
        from baldur.settings.anti_flapping import (
            get_anti_flapping_settings,
            reset_anti_flapping_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_ANTI_FLAPPING_WINDOW_SECONDS": "120",
                "BALDUR_ANTI_FLAPPING_SIMILARITY_THRESHOLD": "0.02",
                "BALDUR_ANTI_FLAPPING_MAX_SIMILAR_CHANGES": "5",
            },
        ):
            reset_anti_flapping_settings()
            settings = get_anti_flapping_settings()

            assert settings.window_seconds == 120
            assert settings.similarity_threshold == 0.02
            assert settings.max_similar_changes == 5


# =============================================================================
# GovernanceSettings Extension Tests
# =============================================================================


class TestGovernanceSettingsExtension:
    """GovernanceSettings 확장 필드 테스트."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.governance import reset_governance_settings

        reset_governance_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.governance import reset_governance_settings

        reset_governance_settings()

    def test_extended_default_values(self):
        """확장된 필드들의 기본값 검증 (governance_checks.py 기반)."""
        from baldur.settings.governance import get_governance_settings

        settings = get_governance_settings()

        # emergency_min_level (check_all_governance 함수 파라미터 기본값)
        assert settings.emergency_min_level == 2

    def test_extended_env_override(self):
        """확장된 필드들의 환경 변수 오버라이드 테스트."""
        from baldur.settings.governance import (
            get_governance_settings,
            reset_governance_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_GOVERNANCE_EMERGENCY_MIN_LEVEL": "3",
            },
        ):
            reset_governance_settings()
            settings = get_governance_settings()

            assert settings.emergency_min_level == 3


# =============================================================================
# SamplingSettings Tests (신규)
# =============================================================================


class TestSamplingSettings:
    """SamplingSettings 테스트 (신규 생성)."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.sampling import reset_sampling_settings

        reset_sampling_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.sampling import reset_sampling_settings

        reset_sampling_settings()

    def test_default_values(self):
        """기본값 검증 (audit/performance/sampling.py SamplingConfig 기반)."""
        from baldur.settings.sampling import get_sampling_settings

        settings = get_sampling_settings()

        assert settings.sample_rate == 0.1  # 10%
        assert settings.min_samples == 10
        assert settings.max_samples == 1000
        assert settings.full_verify_on_failure is True

    def test_env_override(self):
        """환경 변수 오버라이드 테스트."""
        from baldur.settings.sampling import (
            get_sampling_settings,
            reset_sampling_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_SAMPLING_SAMPLE_RATE": "0.2",
                "BALDUR_SAMPLING_MIN_SAMPLES": "20",
                "BALDUR_SAMPLING_MAX_SAMPLES": "2000",
                "BALDUR_SAMPLING_FULL_VERIFY_ON_FAILURE": "false",
            },
        ):
            reset_sampling_settings()
            settings = get_sampling_settings()

            assert settings.sample_rate == 0.2
            assert settings.min_samples == 20
            assert settings.max_samples == 2000
            assert settings.full_verify_on_failure is False

    def test_singleton_pattern(self):
        """싱글톤 패턴 테스트."""
        from baldur.settings.sampling import get_sampling_settings

        settings1 = get_sampling_settings()
        settings2 = get_sampling_settings()

        assert settings1 is settings2

    def test_validation_sample_rate_bounds(self):
        """샘플링 비율 범위 검증."""
        from baldur.settings.sampling import SamplingSettings

        # 유효한 범위
        settings = SamplingSettings(sample_rate=0.5)
        assert settings.sample_rate == 0.5

        # 하한 미만
        with pytest.raises(ValueError):
            SamplingSettings(sample_rate=0.001)  # ge=0.01

        # 상한 초과
        with pytest.raises(ValueError):
            SamplingSettings(sample_rate=1.5)  # le=1.0


# =============================================================================
# SteadyStateSettings Tests (신규)
# =============================================================================


class TestSteadyStateSettings:
    """SteadyStateSettings 테스트 (신규 생성)."""

    def setup_method(self):
        """Reset settings before each test."""
        from baldur.settings.steady_state import reset_steady_state_settings

        reset_steady_state_settings()

    def teardown_method(self):
        """Reset settings after each test."""
        from baldur.settings.steady_state import reset_steady_state_settings

        reset_steady_state_settings()

    def test_default_values(self):
        """기본값 검증 (services/chaos/base/models.py SteadyStateHypothesis 기반)."""
        from baldur.settings.steady_state import get_steady_state_settings

        settings = get_steady_state_settings()

        assert settings.p50_latency_max_ms == 100.0
        assert settings.p99_latency_max_ms == 500.0
        assert settings.error_rate_max_percent == 0.1
        assert settings.throughput_min_rps == 100.0

    def test_env_override(self):
        """환경 변수 오버라이드 테스트."""
        from baldur.settings.steady_state import (
            get_steady_state_settings,
            reset_steady_state_settings,
        )

        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_STEADY_STATE_P50_LATENCY_MAX_MS": "150.0",
                "BALDUR_STEADY_STATE_P99_LATENCY_MAX_MS": "750.0",
                "BALDUR_STEADY_STATE_ERROR_RATE_MAX_PERCENT": "0.5",
                "BALDUR_STEADY_STATE_THROUGHPUT_MIN_RPS": "200.0",
            },
        ):
            reset_steady_state_settings()
            settings = get_steady_state_settings()

            assert settings.p50_latency_max_ms == 150.0
            assert settings.p99_latency_max_ms == 750.0
            assert settings.error_rate_max_percent == 0.5
            assert settings.throughput_min_rps == 200.0

    def test_singleton_pattern(self):
        """싱글톤 패턴 테스트."""
        from baldur.settings.steady_state import get_steady_state_settings

        settings1 = get_steady_state_settings()
        settings2 = get_steady_state_settings()

        assert settings1 is settings2

    def test_validation_latency_bounds(self):
        """레이턴시 범위 검증."""
        from baldur.settings.steady_state import SteadyStateSettings

        # 유효한 범위
        settings = SteadyStateSettings(p50_latency_max_ms=50.0)
        assert settings.p50_latency_max_ms == 50.0

        # 하한 미만
        with pytest.raises(ValueError):
            SteadyStateSettings(p50_latency_max_ms=0.5)  # ge=1.0

    def test_validation_error_rate_bounds(self):
        """에러율 범위 검증."""
        from baldur.settings.steady_state import SteadyStateSettings

        # 상한 초과
        with pytest.raises(ValueError):
            SteadyStateSettings(error_rate_max_percent=150.0)  # le=100.0
