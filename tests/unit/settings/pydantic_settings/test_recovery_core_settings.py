"""
Recovery 핵심 Settings 테스트.

복구 프로세스의 핵심 구성 요소들:
- RecoveryCircuitBreakerSettings: 복구용 서킷브레이커 설정
- RedisKeyGuardSettings: Redis 키 TTL 및 메모리 보호 설정
- RecoveryShutdownSettings: Recovery-aware 종료 설정
- ResilientRecorderSettings: 장애 허용 레코더 설정
"""

import pytest
from pydantic import ValidationError


class TestRecoveryCircuitBreakerSettings:
    """Tests for RecoveryCircuitBreakerSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.recovery_circuit_breaker import (
            reset_recovery_circuit_breaker_settings,
        )

        reset_recovery_circuit_breaker_settings()
        yield
        reset_recovery_circuit_breaker_settings()

    def test_default_values(self):
        """기본값이 recovery_circuit_breaker.py와 일치하는지 검증."""
        from baldur.settings.recovery_circuit_breaker import (
            RecoveryCircuitBreakerSettings,
        )

        settings = RecoveryCircuitBreakerSettings()

        assert settings.error_rate_threshold == 0.15
        assert settings.sampling_window_seconds == 60
        assert settings.min_samples == 10
        assert settings.open_duration_seconds == 300
        assert settings.half_open_max_requests == 5
        assert settings.max_consecutive_trips == 3

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.recovery_circuit_breaker import (
            RecoveryCircuitBreakerSettings,
        )

        monkeypatch.setenv("BALDUR_RECOVERY_CB_ERROR_RATE_THRESHOLD", "0.25")

        settings = RecoveryCircuitBreakerSettings()

        assert settings.error_rate_threshold == 0.25

    def test_validation_error_rate_range(self):
        """error_rate_threshold 범위 (0.01-1.0) 검증."""
        from baldur.settings.recovery_circuit_breaker import (
            RecoveryCircuitBreakerSettings,
        )

        with pytest.raises(ValidationError):
            RecoveryCircuitBreakerSettings(error_rate_threshold=0.0)

        with pytest.raises(ValidationError):
            RecoveryCircuitBreakerSettings(error_rate_threshold=1.5)

    def test_validation_sampling_window_range(self):
        """sampling_window_seconds 범위 (10-600) 검증."""
        from baldur.settings.recovery_circuit_breaker import (
            RecoveryCircuitBreakerSettings,
        )

        with pytest.raises(ValidationError):
            RecoveryCircuitBreakerSettings(sampling_window_seconds=5)

        with pytest.raises(ValidationError):
            RecoveryCircuitBreakerSettings(sampling_window_seconds=1000)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.recovery_circuit_breaker import (
            get_recovery_circuit_breaker_settings,
        )

        settings1 = get_recovery_circuit_breaker_settings()
        settings2 = get_recovery_circuit_breaker_settings()

        assert settings1 is settings2


class TestRedisKeyGuardSettings:
    """Tests for RedisKeyGuardSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.redis_key_guard import reset_redis_key_guard_settings

        reset_redis_key_guard_settings()
        yield
        reset_redis_key_guard_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.redis_key_guard import RedisKeyGuardSettings

        settings = RedisKeyGuardSettings()

        assert settings.memory_warning_threshold == 80.0
        assert settings.memory_critical_threshold == 90.0
        assert settings.target_free_percent == 20.0
        assert settings.cache_ttl_seconds == 3600

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.redis_key_guard import RedisKeyGuardSettings

        monkeypatch.setenv("BALDUR_REDIS_KEY_GUARD_MEMORY_WARNING_THRESHOLD", "75.0")

        settings = RedisKeyGuardSettings()

        assert settings.memory_warning_threshold == 75.0

    def test_validation_memory_threshold_range(self):
        """memory_warning_threshold 범위 검증."""
        from baldur.settings.redis_key_guard import RedisKeyGuardSettings

        with pytest.raises(ValidationError):
            RedisKeyGuardSettings(memory_warning_threshold=40.0)  # < 50

        with pytest.raises(ValidationError):
            RedisKeyGuardSettings(memory_critical_threshold=100.0)  # > 99

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.redis_key_guard import get_redis_key_guard_settings

        settings1 = get_redis_key_guard_settings()
        settings2 = get_redis_key_guard_settings()

        assert settings1 is settings2


class TestRecoveryShutdownSettings:
    """Tests for RecoveryShutdownSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.recovery_shutdown import (
            reset_recovery_shutdown_settings,
        )

        reset_recovery_shutdown_settings()
        yield
        reset_recovery_shutdown_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.recovery_shutdown import RecoveryShutdownSettings

        settings = RecoveryShutdownSettings()

        assert settings.default_drain_timeout_seconds == 30.0
        assert settings.recovery_extension_seconds == 300.0
        assert settings.max_shutdown_wait_seconds == 600.0
        assert settings.recovery_check_interval_seconds == 5.0

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.recovery_shutdown import RecoveryShutdownSettings

        # env_prefix="BALDUR_RECOVERY_SHUTDOWN_" 사용
        monkeypatch.setenv(
            "BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS", "60.0"
        )

        settings = RecoveryShutdownSettings()

        assert settings.default_drain_timeout_seconds == 60.0

    def test_validation_timeout_range(self):
        """timeout 범위 검증."""
        from baldur.settings.recovery_shutdown import RecoveryShutdownSettings

        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(default_drain_timeout_seconds=2.0)  # < 5

        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(max_shutdown_wait_seconds=2000.0)  # > 1800

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.recovery_shutdown import (
            get_recovery_shutdown_settings,
        )

        settings1 = get_recovery_shutdown_settings()
        settings2 = get_recovery_shutdown_settings()

        assert settings1 is settings2


class TestResilientRecorderSettings:
    """Tests for ResilientRecorderSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.resilient_recorder import (
            reset_resilient_recorder_settings,
        )

        reset_resilient_recorder_settings()
        yield
        reset_resilient_recorder_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.resilient_recorder import ResilientRecorderSettings

        settings = ResilientRecorderSettings()

        assert settings.buffer_capacity == 10000
        assert settings.backpressure_strategy == "DROP_OLDEST"
        assert settings.flush_interval_seconds == 1.0
        assert settings.flush_batch_size == 100

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.resilient_recorder import ResilientRecorderSettings

        monkeypatch.setenv("BALDUR_RESILIENT_RECORDER_BUFFER_CAPACITY", "5000")

        settings = ResilientRecorderSettings()

        assert settings.buffer_capacity == 5000

    def test_validation_buffer_capacity_range(self):
        """buffer_capacity 범위 검증."""
        from baldur.settings.resilient_recorder import ResilientRecorderSettings

        with pytest.raises(ValidationError):
            ResilientRecorderSettings(buffer_capacity=50)  # < 100

        with pytest.raises(ValidationError):
            ResilientRecorderSettings(buffer_capacity=2000000)  # > 1000000

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.resilient_recorder import (
            get_resilient_recorder_settings,
        )

        settings1 = get_resilient_recorder_settings()
        settings2 = get_resilient_recorder_settings()

        assert settings1 is settings2
