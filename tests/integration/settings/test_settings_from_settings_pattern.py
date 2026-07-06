"""
from_settings() 패턴 통합 테스트.

from_settings() 팩토리 메서드를 통해 Settings에서 설정값을 읽어
비즈니스 객체를 생성하는 패턴이 올바르게 동작하는지 검증합니다.

Tests:
1. ExponentialBackoff.from_settings()
2. LinearBackoff.from_settings()
3. ConstantBackoff.from_settings()
4. RingBuffer.from_settings()
5. JitterConfig.from_settings()
6. FallbackConfig.from_settings()
7. HashChainCircuitBreakerConfig.from_settings()
8. CanaryWatchdogConfig.from_settings()
"""

import os
from unittest import mock

import pytest


class TestExponentialBackoffFromSettings:
    """ExponentialBackoff.from_settings() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.backoff import reset_backoff_settings

        reset_backoff_settings()
        yield
        reset_backoff_settings()

    def test_default_values(self):
        """기본값으로 인스턴스 생성."""
        from baldur.core.backoff import ExponentialBackoff

        backoff = ExponentialBackoff.from_settings()

        # Matches the core/backoff.py defaults (max_delay aligned with the
        # shared STANDARD_MAX_DELAY of 60s used by the settings layer).
        assert backoff.base_delay == 1.0
        assert backoff.max_delay == 60.0
        assert backoff.multiplier == 2.0
        assert backoff.jitter_factor == 0.2

    def test_settings_override(self):
        """환경변수로 Settings 오버라이드 시 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_BACKOFF_EXPONENTIAL_BASE_DELAY": "2.5",
                "BALDUR_BACKOFF_EXPONENTIAL_MAX_DELAY": "600.0",
                "BALDUR_BACKOFF_EXPONENTIAL_MULTIPLIER": "3.0",
            },
        ):
            from baldur.core.backoff import ExponentialBackoff
            from baldur.settings.backoff import reset_backoff_settings

            reset_backoff_settings()
            backoff = ExponentialBackoff.from_settings()

            assert backoff.base_delay == 2.5
            assert backoff.max_delay == 600.0
            assert backoff.multiplier == 3.0

    def test_override_parameter(self):
        """from_settings()에 직접 오버라이드 파라미터 전달."""
        from baldur.core.backoff import ExponentialBackoff

        backoff = ExponentialBackoff.from_settings(base_delay=5.0, max_delay=100.0)

        assert backoff.base_delay == 5.0
        assert backoff.max_delay == 100.0
        # 오버라이드하지 않은 필드는 Settings 기본값 유지
        assert backoff.multiplier == 2.0


class TestLinearBackoffFromSettings:
    """LinearBackoff.from_settings() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.backoff import reset_backoff_settings

        reset_backoff_settings()
        yield
        reset_backoff_settings()

    def test_default_values(self):
        """기본값으로 인스턴스 생성."""
        from baldur.core.backoff import LinearBackoff

        backoff = LinearBackoff.from_settings()

        assert backoff.base_delay == 1.0
        assert backoff.increment == 1.0
        assert backoff.max_delay == 60.0

    def test_settings_override(self):
        """환경변수로 Settings 오버라이드 시 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_BACKOFF_LINEAR_INCREMENT": "2.5",
            },
        ):
            from baldur.core.backoff import LinearBackoff
            from baldur.settings.backoff import reset_backoff_settings

            reset_backoff_settings()
            backoff = LinearBackoff.from_settings()

            assert backoff.increment == 2.5


class TestConstantBackoffFromSettings:
    """ConstantBackoff.from_settings() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.backoff import reset_backoff_settings

        reset_backoff_settings()
        yield
        reset_backoff_settings()

    def test_default_values(self):
        """기본값으로 인스턴스 생성."""
        from baldur.core.backoff import ConstantBackoff

        backoff = ConstantBackoff.from_settings()

        assert backoff.delay == 5.0

    def test_override_parameter(self):
        """from_settings()에 직접 오버라이드 파라미터 전달."""
        from baldur.core.backoff import ConstantBackoff

        backoff = ConstantBackoff.from_settings(delay=15.0)

        assert backoff.delay == 15.0


class TestRingBufferFromSettings:
    """RingBuffer.from_settings() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.ring_buffer import reset_ring_buffer_settings

        reset_ring_buffer_settings()
        yield
        reset_ring_buffer_settings()

    def test_default_values(self):
        """기본값으로 인스턴스 생성."""
        from baldur.audit.ring_buffer import RingBuffer

        buffer = RingBuffer.from_settings()

        assert buffer.capacity == 10000

    def test_settings_override(self):
        """환경변수로 Settings 오버라이드 시 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_RING_BUFFER_CAPACITY": "5000",
            },
        ):
            from baldur.audit.ring_buffer import RingBuffer
            from baldur.settings.ring_buffer import reset_ring_buffer_settings

            reset_ring_buffer_settings()
            buffer = RingBuffer.from_settings()

            assert buffer.capacity == 5000

    def test_override_parameter(self):
        """from_settings()에 직접 오버라이드 파라미터 전달."""
        from baldur.audit.ring_buffer import RingBuffer

        buffer = RingBuffer.from_settings(capacity=100)

        assert buffer.capacity == 100


class TestJitterConfigFromSettings:
    """JitterConfig.from_settings() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.jitter import reset_jitter_settings

        reset_jitter_settings()
        yield
        reset_jitter_settings()

    def test_default_values(self):
        """기본값으로 인스턴스 생성."""
        from baldur.utils.jitter import JitterConfig

        config = JitterConfig.from_settings()

        assert config.max_delay_seconds == 60.0
        assert config.min_delay_seconds == 0.0
        assert config.enabled is True

    def test_settings_override(self):
        """환경변수로 Settings 오버라이드 시 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_JITTER_MAX_DELAY_SECONDS": "120.0",
                "BALDUR_JITTER_MIN_DELAY_SECONDS": "5.0",
                "BALDUR_JITTER_ENABLED": "false",
            },
        ):
            from baldur.settings.jitter import reset_jitter_settings
            from baldur.utils.jitter import JitterConfig

            reset_jitter_settings()
            config = JitterConfig.from_settings()

            assert config.max_delay_seconds == 120.0
            assert config.min_delay_seconds == 5.0
            assert config.enabled is False


class TestGracefulDegradationFromSettings:
    """FallbackConfig/HashChainCircuitBreakerConfig.from_settings() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.graceful_degradation import (
            reset_graceful_degradation_settings,
        )

        reset_graceful_degradation_settings()
        yield
        reset_graceful_degradation_settings()

    def test_fallback_config_default_values(self):
        """FallbackConfig 기본값으로 인스턴스 생성."""
        from baldur.audit.graceful_degradation.enums import FallbackConfig

        config = FallbackConfig.from_settings()

        assert config.redis_timeout_seconds == 5.0
        assert config.replica_timeout_seconds == 3.0
        assert config.memory_max_entries == 10000

    def test_circuit_breaker_config_default_values(self):
        """HashChainCircuitBreakerConfig 기본값으로 인스턴스 생성."""
        from baldur.audit.graceful_degradation.enums import (
            HashChainCircuitBreakerConfig,
        )

        config = HashChainCircuitBreakerConfig.from_settings()

        assert config.failure_threshold == 5
        assert config.recovery_timeout_seconds == 30.0
        assert config.half_open_requests == 3
        assert config.success_threshold == 2

    def test_fallback_settings_override(self):
        """FallbackConfig 환경변수 오버라이드."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_GRACEFUL_DEGRADATION_REDIS_TIMEOUT_SECONDS": "10.0",
                "BALDUR_GRACEFUL_DEGRADATION_MEMORY_MAX_ENTRIES": "5000",
            },
        ):
            from baldur.audit.graceful_degradation.enums import FallbackConfig
            from baldur.settings.graceful_degradation import (
                reset_graceful_degradation_settings,
            )

            reset_graceful_degradation_settings()
            config = FallbackConfig.from_settings()

            assert config.redis_timeout_seconds == 10.0
            assert config.memory_max_entries == 5000


class TestCanaryWatchdogConfigFromSettings:
    """CanaryWatchdogConfig.from_settings() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.canary_watchdog import reset_canary_watchdog_settings

        reset_canary_watchdog_settings()
        yield
        reset_canary_watchdog_settings()

    def test_default_values(self):
        """기본값으로 인스턴스 생성."""
        from baldur.tasks.canary_watchdog import CanaryWatchdogConfig

        config = CanaryWatchdogConfig.from_settings()

        assert config.zombie_threshold_minutes == 30
        assert config.auto_rollback_after_minutes == 60
        assert config.max_stage_duration_minutes == 15
        assert config.enable_auto_promote is True
        assert config.enable_auto_rollback is True

    def test_settings_override(self):
        """환경변수로 Settings 오버라이드 시 반영."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_CANARY_WATCHDOG_ZOMBIE_THRESHOLD_MINUTES": "45",
                "BALDUR_CANARY_WATCHDOG_AUTO_ROLLBACK_AFTER_MINUTES": "90",
                "BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_ROLLBACK": "true",
            },
        ):
            from baldur.settings.canary_watchdog import (
                reset_canary_watchdog_settings,
            )
            from baldur.tasks.canary_watchdog import CanaryWatchdogConfig

            reset_canary_watchdog_settings()
            config = CanaryWatchdogConfig.from_settings()

            assert config.zombie_threshold_minutes == 45
            assert config.auto_rollback_after_minutes == 90
            assert config.enable_auto_rollback is True


class TestFromSettingsPatternConsistency:
    """from_settings() 패턴의 일관성 검증."""

    def test_all_from_settings_return_correct_type(self):
        """모든 from_settings()가 올바른 타입을 반환하는지 검증."""
        from baldur.audit.graceful_degradation.enums import (
            FallbackConfig,
            HashChainCircuitBreakerConfig,
        )
        from baldur.audit.ring_buffer import RingBuffer
        from baldur.core.backoff import (
            ConstantBackoff,
            ExponentialBackoff,
            LinearBackoff,
        )
        from baldur.tasks.canary_watchdog import CanaryWatchdogConfig
        from baldur.utils.jitter import JitterConfig

        assert isinstance(ExponentialBackoff.from_settings(), ExponentialBackoff)
        assert isinstance(LinearBackoff.from_settings(), LinearBackoff)
        assert isinstance(ConstantBackoff.from_settings(), ConstantBackoff)
        assert isinstance(RingBuffer.from_settings(), RingBuffer)
        assert isinstance(JitterConfig.from_settings(), JitterConfig)
        assert isinstance(FallbackConfig.from_settings(), FallbackConfig)
        assert isinstance(
            HashChainCircuitBreakerConfig.from_settings(), HashChainCircuitBreakerConfig
        )
        assert isinstance(CanaryWatchdogConfig.from_settings(), CanaryWatchdogConfig)

    def test_from_settings_accepts_overrides(self):
        """from_settings()가 overrides 파라미터를 지원하는지 검증."""
        from baldur.core.backoff import ExponentialBackoff

        # 일부 필드만 오버라이드
        backoff = ExponentialBackoff.from_settings(base_delay=10.0)

        assert backoff.base_delay == 10.0
        # 오버라이드하지 않은 필드는 Settings 기본값
        assert backoff.multiplier == 2.0
