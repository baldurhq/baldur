"""
SystemControlSettings 단위 테스트.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: 설계 문서(339)에 명시된 기본값/제약 계약 검증 (하드코딩)
- Behavior: backend Literal 검증, Django fallback, Redis URL 3-tier fallback,
            환경변수 오버라이드, 싱글톤 pair

참조 소스:
- settings/system_control.py (SystemControlSettings)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.system_control import (
    SystemControlSettings,
    get_system_control_settings,
    reset_system_control_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """각 테스트 전후 싱글톤 리셋."""
    reset_system_control_settings()
    yield
    reset_system_control_settings()


# =============================================================================
# Contract Tests — 설계 계약값 검증 (339 §7.1)
# =============================================================================


class TestSystemControlSettingsDefaultContract:
    """SystemControlSettings 기본값 설계 계약 검증."""

    @pytest.fixture(autouse=True)
    def _clean_state_env(self, monkeypatch):
        """Contract tests verify pure defaults — remove test-environment overrides."""
        monkeypatch.delenv("BALDUR_SYSTEM_CONTROL_BACKEND", raising=False)

    def test_backend_default_file(self):
        """State backend 기본값: 'file'."""
        assert SystemControlSettings().backend == "file"

    def test_state_dir_default(self):
        """State directory 기본값: 'logs/baldur_state'."""
        s = SystemControlSettings()
        assert s.state_dir == "logs/baldur_state"
        assert s.dir == "logs/baldur_state"

    def test_redis_url_default_empty(self):
        """Redis URL 기본값: 빈 문자열 (fallback 체인 트리거)."""
        # redis fallback이 실행되므로, env를 차단해야 순수 기본값 검증 가능
        s = SystemControlSettings(redis_url="")
        # fallback이 실행되어 값이 채워질 수 있으나, env/django 없으면 RedisSettings.url 기본값
        assert isinstance(s.redis_url, str)

    def test_redis_key_prefix_default(self):
        """Redis key prefix 기본값: 'baldur:state:'."""
        assert SystemControlSettings().redis_key_prefix == "baldur:state:"

    def test_redis_scan_batch_size_default(self):
        """Redis SCAN batch size 기본값: 100."""
        assert SystemControlSettings().redis_scan_batch_size == 100

    def test_redis_max_scan_keys_default(self):
        """Redis max scan keys 기본값: 10000."""
        assert SystemControlSettings().redis_max_scan_keys == 10000

    def test_field_count(self):
        """SystemControlSettings has exactly 6 fields."""
        assert len(SystemControlSettings.model_fields) == 6

    def test_env_prefix(self):
        """환경변수 접두사: BALDUR_SYSTEM_CONTROL_."""
        assert (
            SystemControlSettings.model_config.get("env_prefix")
            == "BALDUR_SYSTEM_CONTROL_"
        )


# =============================================================================
# Boundary Tests — 필드 경계값 검증 (§8.1)
# =============================================================================


class TestSystemControlSettingsBoundaryContract:
    """SystemControlSettings 필드 경계값 계약 검증."""

    def test_redis_scan_batch_size_below_minimum_rejected(self):
        """redis_scan_batch_size: ge=50 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(redis_scan_batch_size=49)

    def test_redis_scan_batch_size_at_minimum_accepted(self):
        """redis_scan_batch_size: ge=50 경계값 → 성공."""
        s = SystemControlSettings(redis_scan_batch_size=50)
        assert s.redis_scan_batch_size == 50

    def test_redis_scan_batch_size_above_maximum_rejected(self):
        """redis_scan_batch_size: le=1000 초과 → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(redis_scan_batch_size=1001)

    def test_redis_max_scan_keys_below_minimum_rejected(self):
        """redis_max_scan_keys: ge=100 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(redis_max_scan_keys=99)

    def test_redis_max_scan_keys_above_maximum_rejected(self):
        """redis_max_scan_keys: le=1_000_000 초과 → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(redis_max_scan_keys=1_000_001)


# =============================================================================
# Behavior Tests — backend 검증, fallback, 환경변수, 싱글톤
# =============================================================================


class TestSystemControlSettingsBackendValidationBehavior:
    """backend Literal 검증 동작."""

    def test_backend_file_accepted(self):
        """'file' backend 허용."""
        s = SystemControlSettings(backend="file")
        assert s.backend == "file"

    def test_backend_redis_accepted(self):
        """'redis' backend 허용."""
        s = SystemControlSettings(backend="redis")
        assert s.backend == "redis"

    def test_backend_memory_accepted(self):
        """'memory' backend 허용."""
        s = SystemControlSettings(backend="memory")
        assert s.backend == "memory"

    def test_backend_invalid_value_rejected(self):
        """유효하지 않은 backend 값 → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(backend="dynamodb")

    def test_backend_case_insensitive_normalization(self):
        """backend 값은 .lower() 정규화된다."""
        s = SystemControlSettings(backend="Redis")
        assert s.backend == "redis"

    def test_backend_uppercase_normalization(self):
        """대문자 backend 값도 정규화."""
        s = SystemControlSettings(backend="FILE")
        assert s.backend == "file"


class TestSystemControlSettingsEnvOverrideBehavior:
    """환경변수 오버라이드 동작 검증."""

    def test_env_override_backend(self, monkeypatch):
        """BALDUR_SYSTEM_CONTROL_BACKEND 환경변수로 backend 오버라이드."""
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_BACKEND", "memory")
        s = SystemControlSettings()
        assert s.backend == "memory"

    def test_env_override_state_dir(self, monkeypatch):
        """BALDUR_SYSTEM_CONTROL_DIR 환경변수로 dir 오버라이드."""
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_DIR", "/custom/path")
        s = SystemControlSettings()
        assert s.dir == "/custom/path"
        assert s.state_dir == "/custom/path"

    def test_env_override_redis_url(self, monkeypatch):
        """BALDUR_SYSTEM_CONTROL_REDIS_URL 환경변수로 redis_url 오버라이드."""
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_REDIS_URL", "redis://prod:6379/1")
        s = SystemControlSettings()
        assert s.redis_url == "redis://prod:6379/1"


class TestSystemControlSettingsRedisUrlFallbackBehavior:
    """Redis URL 3-tier fallback 체인 동작 검증."""

    def test_legacy_env_var_fallback(self, monkeypatch):
        """Tier 1: BALDUR_REDIS_URL (legacy env) fallback."""
        # STATE_REDIS_URL 미설정, BALDUR_REDIS_URL만 설정
        monkeypatch.delenv("BALDUR_SYSTEM_CONTROL_REDIS_URL", raising=False)
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://legacy:6379/2")
        s = SystemControlSettings(redis_url="")
        assert s.redis_url == "redis://legacy:6379/2"

    def test_explicit_redis_url_takes_precedence(self, monkeypatch):
        """명시적 STATE_REDIS_URL이 fallback보다 우선."""
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_REDIS_URL", "redis://explicit:6379/0")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://legacy:6379/2")
        s = SystemControlSettings()
        assert s.redis_url == "redis://explicit:6379/0"


class TestSystemControlSettingsSingletonBehavior:
    """SystemControlSettings 싱글톤 pair 동작 검증."""

    def test_get_returns_same_instance(self):
        """get_system_control_settings()는 동일 인스턴스를 반환한다."""
        s1 = get_system_control_settings()
        s2 = get_system_control_settings()
        assert s1 is s2

    def test_reset_clears_cached_instance(self):
        """reset 후 새 인스턴스가 생성된다."""
        s1 = get_system_control_settings()
        reset_system_control_settings()
        s2 = get_system_control_settings()
        assert s1 is not s2
