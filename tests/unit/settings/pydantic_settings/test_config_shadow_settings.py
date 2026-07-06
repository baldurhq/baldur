"""
Tests for ConfigShadowSettings — Shadow Gate configuration.

Covers: contract defaults, boundary analysis, singleton lifecycle, env override.
Target: settings/config_shadow.py (commit 300)
"""

import pytest
from pydantic import ValidationError


class TestConfigShadowSettingsContract:
    """ConfigShadowSettings 설계 계약값 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.config_shadow import reset_config_shadow_settings

        reset_config_shadow_settings()
        yield
        reset_config_shadow_settings()

    def test_gate_enabled_default_is_true(self):
        """gate_enabled 기본값: True."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings()
        assert settings.gate_enabled is True

    def test_require_evaluation_default_is_false(self):
        """require_evaluation 기본값: False."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings()
        assert settings.require_evaluation is False

    def test_default_time_window_hours_contract_value(self):
        """default_time_window_hours 기본값: 336 (14일)."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings()
        assert settings.default_time_window_hours == 336

    def test_min_confidence_contract_value(self):
        """min_confidence 기본값: 0.3."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings()
        assert settings.min_confidence == pytest.approx(0.3)

    def test_bypass_min_reason_length_contract_value(self):
        """bypass_min_reason_length 기본값: 10."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings()
        assert settings.bypass_min_reason_length == 10

    def test_evaluation_ttl_hours_contract_value(self):
        """evaluation_ttl_hours 기본값: 1.0."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings()
        assert settings.evaluation_ttl_hours == pytest.approx(1.0)

    def test_block_on_low_confidence_default_is_false(self):
        """block_on_low_confidence 기본값: False."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings()
        assert settings.block_on_low_confidence is False

    def test_confidence_graduation_target_events_contract_value(self):
        """confidence_graduation_target_events 기본값: 50."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings()
        assert settings.confidence_graduation_target_events == 50

    def test_live_evaluation_enabled_default_is_false(self):
        """live_evaluation_enabled 기본값: False."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings()
        assert settings.live_evaluation_enabled is False

    def test_env_prefix_is_baldur_shadow(self):
        """env_prefix가 BALDUR_CONFIG_SHADOW_이다."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        assert (
            ConfigShadowSettings.model_config["env_prefix"] == "BALDUR_CONFIG_SHADOW_"
        )


class TestConfigShadowSettingsBoundary:
    """ConfigShadowSettings 경계값 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        from baldur.settings.config_shadow import reset_config_shadow_settings

        reset_config_shadow_settings()
        yield
        reset_config_shadow_settings()

    # --- default_time_window_hours: ge=24, le=720 ---

    def test_time_window_hours_below_minimum_raises_validation_error(self):
        """default_time_window_hours < 24 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(default_time_window_hours=23)

    def test_time_window_hours_at_minimum_boundary_succeeds(self):
        """default_time_window_hours = 24 시 성공."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings(default_time_window_hours=24)
        assert settings.default_time_window_hours == 24

    def test_time_window_hours_at_maximum_boundary_succeeds(self):
        """default_time_window_hours = 720 시 성공."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings(default_time_window_hours=720)
        assert settings.default_time_window_hours == 720

    def test_time_window_hours_above_maximum_raises_validation_error(self):
        """default_time_window_hours > 720 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(default_time_window_hours=721)

    # --- min_confidence: ge=0.0, le=1.0 ---

    def test_min_confidence_at_zero_boundary_succeeds(self):
        """min_confidence = 0.0 시 성공."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings(min_confidence=0.0)
        assert settings.min_confidence == pytest.approx(0.0)

    def test_min_confidence_at_one_boundary_succeeds(self):
        """min_confidence = 1.0 시 성공."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings(min_confidence=1.0)
        assert settings.min_confidence == pytest.approx(1.0)

    def test_min_confidence_below_zero_raises_validation_error(self):
        """min_confidence < 0.0 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(min_confidence=-0.01)

    def test_min_confidence_above_one_raises_validation_error(self):
        """min_confidence > 1.0 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(min_confidence=1.01)

    # --- bypass_min_reason_length: ge=5, le=500 ---

    def test_bypass_min_reason_length_below_minimum_raises_validation_error(self):
        """bypass_min_reason_length < 5 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(bypass_min_reason_length=4)

    def test_bypass_min_reason_length_at_minimum_boundary_succeeds(self):
        """bypass_min_reason_length = 5 시 성공."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings(bypass_min_reason_length=5)
        assert settings.bypass_min_reason_length == 5

    def test_bypass_min_reason_length_above_maximum_raises_validation_error(self):
        """bypass_min_reason_length > 500 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(bypass_min_reason_length=501)

    # --- evaluation_ttl_hours: ge=0.25, le=24.0 ---

    def test_evaluation_ttl_below_minimum_raises_validation_error(self):
        """evaluation_ttl_hours < 0.25 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(evaluation_ttl_hours=0.24)

    def test_evaluation_ttl_at_minimum_boundary_succeeds(self):
        """evaluation_ttl_hours = 0.25 시 성공."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        settings = ConfigShadowSettings(evaluation_ttl_hours=0.25)
        assert settings.evaluation_ttl_hours == pytest.approx(0.25)

    def test_evaluation_ttl_above_maximum_raises_validation_error(self):
        """evaluation_ttl_hours > 24.0 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(evaluation_ttl_hours=24.01)

    # --- confidence_graduation_target_events: ge=20, le=500 ---

    def test_graduation_target_events_below_minimum_raises_validation_error(self):
        """confidence_graduation_target_events < 20 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(confidence_graduation_target_events=19)

    def test_graduation_target_events_above_maximum_raises_validation_error(self):
        """confidence_graduation_target_events > 500 시 ValidationError."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        with pytest.raises(ValidationError):
            ConfigShadowSettings(confidence_graduation_target_events=501)


class TestConfigShadowSettingsBehavior:
    """ConfigShadowSettings 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        from baldur.settings.config_shadow import reset_config_shadow_settings

        reset_config_shadow_settings()
        yield
        reset_config_shadow_settings()

    def test_env_override_gate_enabled(self, monkeypatch):
        """환경변수 BALDUR_CONFIG_SHADOW_GATE_ENABLED으로 오버라이드."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        monkeypatch.setenv("BALDUR_CONFIG_SHADOW_GATE_ENABLED", "false")
        settings = ConfigShadowSettings()
        assert settings.gate_enabled is False

    def test_env_override_min_confidence(self, monkeypatch):
        """환경변수 BALDUR_CONFIG_SHADOW_MIN_CONFIDENCE으로 오버라이드."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        monkeypatch.setenv("BALDUR_CONFIG_SHADOW_MIN_CONFIDENCE", "0.8")
        settings = ConfigShadowSettings()
        assert settings.min_confidence == pytest.approx(0.8)

    def test_env_override_live_evaluation_enabled(self, monkeypatch):
        """환경변수 BALDUR_CONFIG_SHADOW_LIVE_EVALUATION_ENABLED으로 오버라이드."""
        from baldur.settings.config_shadow import ConfigShadowSettings

        monkeypatch.setenv("BALDUR_CONFIG_SHADOW_LIVE_EVALUATION_ENABLED", "true")
        settings = ConfigShadowSettings()
        assert settings.live_evaluation_enabled is True

    def test_singleton_returns_same_instance(self):
        """get_config_shadow_settings()는 동일 인스턴스를 반환."""
        from baldur.settings.config_shadow import get_config_shadow_settings

        first = get_config_shadow_settings()
        second = get_config_shadow_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset 후 새 인스턴스가 생성된다."""
        from baldur.settings.config_shadow import (
            get_config_shadow_settings,
            reset_config_shadow_settings,
        )

        first = get_config_shadow_settings()
        reset_config_shadow_settings()
        second = get_config_shadow_settings()
        assert first is not second
