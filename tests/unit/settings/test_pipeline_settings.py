"""
PipelineSettings 단위 테스트.

테스트 대상:
- settings/pipeline.py (PipelineSettings, get/reset singleton)

검증 기법:
- 계약 검증: 기본값, 환경변수 prefix
- 경계값 분석: audit_sampling_rate ge=0.0, le=1.0
- 싱글톤 라이프사이클: get/reset
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.pipeline import (
    PipelineSettings,
    get_pipeline_settings,
    reset_pipeline_settings,
)

# =============================================================================
# 계약 검증 — PipelineSettings
# =============================================================================


class TestPipelineSettingsContract:
    """PipelineSettings 설계 계약값 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_pipeline_settings()
        yield
        reset_pipeline_settings()

    def test_adaptive_enabled_default_false(self):
        """adaptive_enabled 기본값은 False이다."""
        settings = PipelineSettings()
        assert settings.adaptive_enabled is False

    def test_hot_path_tiers_default(self):
        """hot_path_tiers 기본값은 ["non_essential"]이다."""
        settings = PipelineSettings()
        assert settings.hot_path_tiers == ["non_essential"]

    def test_audit_sampling_rate_default_one(self):
        """audit_sampling_rate 기본값은 1.0 (100% 감사)이다."""
        settings = PipelineSettings()
        assert settings.audit_sampling_rate == 1.0

    def test_env_prefix(self):
        """환경변수 prefix는 BALDUR_PIPELINE_이다."""
        config = PipelineSettings.model_config
        assert config["env_prefix"] == "BALDUR_PIPELINE_"


# =============================================================================
# 동작 검증 — 경계값 분석
# =============================================================================


class TestPipelineSettingsBoundaryBehavior:
    """PipelineSettings 경계값 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_pipeline_settings()
        yield
        reset_pipeline_settings()

    def test_audit_sampling_rate_zero_accepted(self):
        """audit_sampling_rate=0.0은 허용된다."""
        settings = PipelineSettings(audit_sampling_rate=0.0)
        assert settings.audit_sampling_rate == 0.0

    def test_audit_sampling_rate_one_accepted(self):
        """audit_sampling_rate=1.0은 허용된다."""
        settings = PipelineSettings(audit_sampling_rate=1.0)
        assert settings.audit_sampling_rate == 1.0

    def test_audit_sampling_rate_below_zero_rejected(self):
        """audit_sampling_rate < 0.0이면 ValidationError가 발생한다."""
        with pytest.raises(ValidationError):
            PipelineSettings(audit_sampling_rate=-0.01)

    def test_audit_sampling_rate_above_one_rejected(self):
        """audit_sampling_rate > 1.0이면 ValidationError가 발생한다."""
        with pytest.raises(ValidationError):
            PipelineSettings(audit_sampling_rate=1.01)

    def test_audit_sampling_rate_mid_accepted(self):
        """audit_sampling_rate=0.5는 허용된다."""
        settings = PipelineSettings(audit_sampling_rate=0.5)
        assert settings.audit_sampling_rate == 0.5


# =============================================================================
# 동작 검증 — 싱글톤 라이프사이클
# =============================================================================


class TestPipelineSettingsSingletonBehavior:
    """PipelineSettings 싱글톤 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_pipeline_settings()
        yield
        reset_pipeline_settings()

    def test_get_returns_same_instance(self):
        """get_pipeline_settings()는 동일한 인스턴스를 반환한다."""
        first = get_pipeline_settings()
        second = get_pipeline_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset_pipeline_settings() 후 새 인스턴스가 반환된다."""
        first = get_pipeline_settings()
        reset_pipeline_settings()
        second = get_pipeline_settings()
        assert first is not second

    def test_get_returns_pipeline_settings_type(self):
        """get_pipeline_settings()는 PipelineSettings 인스턴스를 반환한다."""
        settings = get_pipeline_settings()
        assert isinstance(settings, PipelineSettings)
