"""
HealthCheckSettings 단위 테스트.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: 설계 문서(339)에 명시된 기본값/제약 계약 검증 (하드코딩)
- Behavior: 환경변수 오버라이드, 싱글톤 pair, 경계값 동작 검증

참조 소스:
- settings/health_check.py (HealthCheckSettings)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.health_check import (
    HealthCheckSettings,
    get_health_check_settings,
    reset_health_check_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """각 테스트 전후 싱글톤 리셋."""
    reset_health_check_settings()
    yield
    reset_health_check_settings()


# =============================================================================
# Contract Tests — 설계 계약값 검증 (339 §5.1)
# =============================================================================


class TestHealthCheckSettingsDefaultContract:
    """HealthCheckSettings 기본값 설계 계약 검증."""

    def test_checker_cache_ttl_seconds_default(self):
        """TTLCacheStrategy 기본 캐시 TTL: 5.0초."""
        assert HealthCheckSettings().checker_cache_ttl_seconds == 5.0

    def test_tcp_info_timeout_seconds_default(self):
        """LinuxTCPInfoStrategy 타임아웃: 0.1초."""
        assert HealthCheckSettings().tcp_info_timeout_seconds == 0.1

    def test_socket_timeout_seconds_default(self):
        """SimpleSocketStrategy 타임아웃: 1.0초."""
        assert HealthCheckSettings().socket_timeout_seconds == 1.0

    def test_probe_cb_open_threshold_default(self):
        """CB OPEN DEGRADED 임계치: 3."""
        assert HealthCheckSettings().probe_cb_open_threshold == 3

    def test_probe_active_recoveries_threshold_default(self):
        """활성 복구 DEGRADED 임계치: 10."""
        assert HealthCheckSettings().probe_active_recoveries_threshold == 10

    def test_probe_memory_usage_threshold_default(self):
        """Redis 메모리 DEGRADED 임계치: 0.8."""
        assert HealthCheckSettings().probe_memory_usage_threshold == pytest.approx(0.8)

    def test_probe_worker_join_timeout_default(self):
        """워커 스레드 join 타임아웃: 2.0초."""
        assert HealthCheckSettings().probe_worker_join_timeout == 2.0

    def test_field_count(self):
        """HealthCheckSettings has exactly 10 fields."""
        assert len(HealthCheckSettings.model_fields) == 10

    def test_env_prefix(self):
        """환경변수 접두사: BALDUR_HEALTH_CHECK_."""
        assert (
            HealthCheckSettings.model_config.get("env_prefix") == "BALDUR_HEALTH_CHECK_"
        )


# =============================================================================
# Boundary Tests — 필드 경계값 검증 (§8.1)
# =============================================================================


class TestHealthCheckSettingsBoundaryContract:
    """HealthCheckSettings 필드 경계값 계약 검증."""

    def test_checker_cache_ttl_below_minimum_rejected(self):
        """checker_cache_ttl_seconds: ge=0.5 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(checker_cache_ttl_seconds=0.4)

    def test_checker_cache_ttl_at_minimum_accepted(self):
        """checker_cache_ttl_seconds: ge=0.5 경계값 → 성공."""
        s = HealthCheckSettings(checker_cache_ttl_seconds=0.5)
        assert s.checker_cache_ttl_seconds == 0.5

    def test_checker_cache_ttl_above_maximum_rejected(self):
        """checker_cache_ttl_seconds: le=60.0 초과 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(checker_cache_ttl_seconds=60.1)

    def test_probe_cb_open_threshold_below_minimum_rejected(self):
        """probe_cb_open_threshold: ge=1 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(probe_cb_open_threshold=0)

    def test_probe_memory_usage_threshold_above_maximum_rejected(self):
        """probe_memory_usage_threshold: le=1.0 초과 → ValidationError."""
        with pytest.raises(ValidationError):
            HealthCheckSettings(probe_memory_usage_threshold=1.1)

    def test_probe_memory_usage_threshold_at_minimum_accepted(self):
        """probe_memory_usage_threshold: ge=0.1 경계값 → 성공."""
        s = HealthCheckSettings(probe_memory_usage_threshold=0.1)
        assert s.probe_memory_usage_threshold == pytest.approx(0.1)


# =============================================================================
# Behavior Tests — 환경변수 오버라이드, 싱글톤
# =============================================================================


class TestHealthCheckSettingsEnvOverrideBehavior:
    """환경변수 오버라이드 동작 검증."""

    def test_env_override_checker_cache_ttl(self, monkeypatch):
        """BALDUR_HEALTH_CHECK_CHECKER_CACHE_TTL_SECONDS 환경변수로 오버라이드."""
        monkeypatch.setenv("BALDUR_HEALTH_CHECK_CHECKER_CACHE_TTL_SECONDS", "10.0")
        s = HealthCheckSettings()
        assert s.checker_cache_ttl_seconds == 10.0

    def test_env_override_probe_cb_open_threshold(self, monkeypatch):
        """BALDUR_HEALTH_CHECK_PROBE_CB_OPEN_THRESHOLD 환경변수로 오버라이드."""
        monkeypatch.setenv("BALDUR_HEALTH_CHECK_PROBE_CB_OPEN_THRESHOLD", "5")
        s = HealthCheckSettings()
        assert s.probe_cb_open_threshold == 5


class TestHealthCheckSettingsSingletonBehavior:
    """HealthCheckSettings 싱글톤 pair 동작 검증."""

    def test_get_returns_same_instance(self):
        """get_health_check_settings()는 동일 인스턴스를 반환한다."""
        s1 = get_health_check_settings()
        s2 = get_health_check_settings()
        assert s1 is s2

    def test_reset_clears_cached_instance(self):
        """reset 후 새 인스턴스가 생성된다."""
        s1 = get_health_check_settings()
        reset_health_check_settings()
        s2 = get_health_check_settings()
        assert s1 is not s2
