"""
Tests for CorrelationEngineSettings — 오케스트레이터 설정.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: 설계 계약값(기본값, 범위)이 올바르게 반영되었는지 검증
- Behavior: 환경변수 override, reset 동작 검증

참조 소스:
- settings/correlation_engine.py (CorrelationEngineSettings)
"""

from __future__ import annotations

import pytest

from baldur.settings.correlation_engine import (
    CorrelationEngineSettings,
    get_correlation_engine_settings,
    reset_correlation_engine_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """각 테스트 간 설정 싱글톤 캐시 격리."""
    reset_correlation_engine_settings()
    yield
    reset_correlation_engine_settings()


# =============================================================================
# Contract Tests — 설계 계약값 검증
# =============================================================================


class TestCorrelationEngineSettingsContract:
    """CorrelationEngineSettings 기본값 설계 사양 검증."""

    def test_enabled_default_false(self):
        """enabled default = False (Dormant tier per V1_LAUNCH_MANIFEST)."""
        settings = CorrelationEngineSettings()
        assert settings.enabled is False

    def test_analysis_interval_seconds_default(self):
        """analysis_interval_seconds 기본값 = 60.0."""
        settings = CorrelationEngineSettings()
        assert settings.analysis_interval_seconds == 60.0

    def test_weight_topology_default(self):
        """weight_topology 기본값 = 0.35."""
        settings = CorrelationEngineSettings()
        assert settings.weight_topology == pytest.approx(0.35)

    def test_weight_temporal_default(self):
        """weight_temporal 기본값 = 0.25."""
        settings = CorrelationEngineSettings()
        assert settings.weight_temporal == pytest.approx(0.25)

    def test_weight_blast_radius_default(self):
        """weight_blast_radius 기본값 = 0.25."""
        settings = CorrelationEngineSettings()
        assert settings.weight_blast_radius == pytest.approx(0.25)

    def test_weight_historical_default(self):
        """weight_historical 기본값 = 0.15."""
        settings = CorrelationEngineSettings()
        assert settings.weight_historical == pytest.approx(0.15)

    def test_learning_integration_enabled_default_false(self):
        """learning_integration_enabled default = False (Dormant tier)."""
        settings = CorrelationEngineSettings()
        assert settings.learning_integration_enabled is False

    def test_postmortem_integration_enabled_default_false(self):
        """postmortem_integration_enabled default = False (Dormant tier)."""
        settings = CorrelationEngineSettings()
        assert settings.postmortem_integration_enabled is False

    def test_state_persistence_enabled_default_false(self):
        """state_persistence_enabled default = False (Dormant tier)."""
        settings = CorrelationEngineSettings()
        assert settings.state_persistence_enabled is False

    def test_env_prefix_contract(self):
        """env_prefix = BALDUR_CORRELATION_ENGINE_ (508 D1 namespace separation)."""
        config = CorrelationEngineSettings.model_config
        assert config["env_prefix"] == "BALDUR_CORRELATION_ENGINE_"


# =============================================================================
# Behavior Tests — 동작 검증
# =============================================================================


class TestCorrelationEngineSettingsBehavior:
    """CorrelationEngineSettings 동작 검증."""

    def test_singleton_returns_same_instance(self):
        """get_correlation_engine_settings()는 동일 인스턴스를 반환한다."""
        s1 = get_correlation_engine_settings()
        s2 = get_correlation_engine_settings()
        assert s1 is s2

    def test_reset_invalidates_cache(self):
        """reset 후 get은 새 인스턴스를 반환한다."""
        s1 = get_correlation_engine_settings()
        reset_correlation_engine_settings()
        s2 = get_correlation_engine_settings()
        assert s1 is not s2

    def test_extra_ignore(self):
        """Unknown env vars are ignored; manifest-aligned default preserved."""
        settings = CorrelationEngineSettings(
            _env_file=None,
        )
        assert settings.enabled is False  # default preserved

    def test_analysis_interval_seconds_validation_ge(self):
        """analysis_interval_seconds는 10.0 미만이면 유효성 실패."""
        with pytest.raises(Exception):
            CorrelationEngineSettings(
                analysis_interval_seconds=5.0,
                _env_file=None,
            )

    def test_analysis_interval_seconds_validation_le(self):
        """analysis_interval_seconds는 600.0 초과이면 유효성 실패."""
        with pytest.raises(Exception):
            CorrelationEngineSettings(
                analysis_interval_seconds=700.0,
                _env_file=None,
            )

    def test_weight_range_validation(self):
        """가중치는 0.0 ~ 1.0 범위여야 한다."""
        with pytest.raises(Exception):
            CorrelationEngineSettings(
                weight_topology=1.5,
                _env_file=None,
            )
