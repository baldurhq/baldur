"""
Settings 환경변수 오버라이드 통합 테스트.

여러 Settings 모듈에서 환경변수로 설정값을 오버라이드할 때,
실제 비즈니스 로직에 올바르게 반영되는지 검증합니다.

Tests:
1. StressTestSettings → StressTestService 연동
2. CleanupSettings → CleanupService 연동
3. JitterSettings → JitterConfig 연동
4. PoolMonitorSettings → PoolStats 연동
5. BackoffSettings → Backoff 클래스들 연동
"""

import os
from unittest import mock

import pytest


class TestStressTestSettingsEnvOverride:
    """StressTestSettings 환경변수가 StressTestService에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.stress_test import reset_stress_test_settings

        reset_stress_test_settings()
        yield
        reset_stress_test_settings()

    def test_default_lock_timeout_ms_override(self):
        """DEFAULT_LOCK_TIMEOUT_MS 환경변수가 StressTestService에 반영."""
        with mock.patch.dict(
            os.environ, {"BALDUR_STRESS_TEST_DEFAULT_LOCK_TIMEOUT_MS": "5"}
        ):
            from baldur.settings.stress_test import (
                get_stress_test_settings,
                reset_stress_test_settings,
            )

            reset_stress_test_settings()
            settings = get_stress_test_settings()

            assert settings.default_lock_timeout_ms == 5

    def test_multiple_fields_override(self):
        """여러 필드를 동시에 오버라이드할 수 있는지 검증."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_STRESS_TEST_DEFAULT_LOCK_TIMEOUT_MS": "3",
                "BALDUR_STRESS_TEST_MAX_BURST_DURATION_SECONDS": "45",
                "BALDUR_STRESS_TEST_MAX_CONCURRENT_LOCKS": "150",
            },
        ):
            from baldur.settings.stress_test import (
                get_stress_test_settings,
                reset_stress_test_settings,
            )

            reset_stress_test_settings()
            settings = get_stress_test_settings()

            assert settings.default_lock_timeout_ms == 3
            assert settings.max_burst_duration_seconds == 45
            assert settings.max_concurrent_locks == 150


class TestCleanupSettingsEnvOverride:
    """CleanupSettings 환경변수가 CleanupService에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.cleanup import reset_cleanup_settings

        reset_cleanup_settings()
        yield
        reset_cleanup_settings()

    def test_archive_older_than_days_override(self):
        """ARCHIVE_OLDER_THAN_DAYS 환경변수 반영 검증."""
        with mock.patch.dict(
            os.environ, {"BALDUR_CLEANUP_ARCHIVE_OLDER_THAN_DAYS": "60"}
        ):
            from baldur.settings.cleanup import (
                get_cleanup_settings,
                reset_cleanup_settings,
            )

            reset_cleanup_settings()
            settings = get_cleanup_settings()

            assert settings.archive_older_than_days == 60

    def test_purge_older_than_days_override(self):
        """PURGE_OLDER_THAN_DAYS 환경변수 반영 검증."""
        with mock.patch.dict(
            os.environ, {"BALDUR_CLEANUP_PURGE_OLDER_THAN_DAYS": "180"}
        ):
            from baldur.settings.cleanup import (
                get_cleanup_settings,
                reset_cleanup_settings,
            )

            reset_cleanup_settings()
            settings = get_cleanup_settings()

            assert settings.purge_older_than_days == 180


class TestJitterSettingsEnvOverride:
    """JitterSettings 환경변수가 JitterConfig에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.jitter import reset_jitter_settings

        reset_jitter_settings()
        yield
        reset_jitter_settings()

    def test_max_delay_seconds_override(self):
        """MAX_DELAY_SECONDS 환경변수가 JitterConfig.from_settings()에 반영."""
        with mock.patch.dict(os.environ, {"BALDUR_JITTER_MAX_DELAY_SECONDS": "120.0"}):
            from baldur.settings.jitter import reset_jitter_settings
            from baldur.utils.jitter import JitterConfig

            reset_jitter_settings()
            config = JitterConfig.from_settings()

            assert config.max_delay_seconds == 120.0

    def test_enabled_override(self):
        """ENABLED 환경변수가 JitterConfig에 반영."""
        with mock.patch.dict(os.environ, {"BALDUR_JITTER_ENABLED": "false"}):
            from baldur.settings.jitter import reset_jitter_settings
            from baldur.utils.jitter import JitterConfig

            reset_jitter_settings()
            config = JitterConfig.from_settings()

            assert config.enabled is False


class TestPoolMonitorSettingsEnvOverride:
    """PoolMonitorSettings 환경변수가 PoolMonitor에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.pool_monitor import reset_pool_monitor_settings

        reset_pool_monitor_settings()
        yield
        reset_pool_monitor_settings()

    def test_warning_threshold_override(self):
        """WARNING_THRESHOLD 환경변수 반영 검증."""
        with mock.patch.dict(
            os.environ, {"BALDUR_POOL_MONITOR_WARNING_THRESHOLD": "80.0"}
        ):
            from baldur.settings.pool_monitor import (
                get_pool_monitor_settings,
                reset_pool_monitor_settings,
            )

            reset_pool_monitor_settings()
            settings = get_pool_monitor_settings()

            assert settings.warning_threshold == 80.0

    def test_critical_threshold_override(self):
        """CRITICAL_THRESHOLD 환경변수 반영 검증."""
        with mock.patch.dict(
            os.environ, {"BALDUR_POOL_MONITOR_CRITICAL_THRESHOLD": "95.0"}
        ):
            from baldur.settings.pool_monitor import (
                get_pool_monitor_settings,
                reset_pool_monitor_settings,
            )

            reset_pool_monitor_settings()
            settings = get_pool_monitor_settings()

            assert settings.critical_threshold == 95.0


class TestBackoffSettingsEnvOverride:
    """BackoffSettings 환경변수가 Backoff 클래스들에 반영되는지 검증."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset settings before and after each test."""
        from baldur.settings.backoff import reset_backoff_settings

        reset_backoff_settings()
        yield
        reset_backoff_settings()

    def test_exponential_base_delay_override(self):
        """EXPONENTIAL_BASE_DELAY 환경변수가 ExponentialBackoff에 반영."""
        with mock.patch.dict(
            os.environ, {"BALDUR_BACKOFF_EXPONENTIAL_BASE_DELAY": "2.0"}
        ):
            from baldur.core.backoff import ExponentialBackoff
            from baldur.settings.backoff import reset_backoff_settings

            reset_backoff_settings()
            backoff = ExponentialBackoff.from_settings()

            assert backoff.base_delay == 2.0

    def test_linear_increment_override(self):
        """LINEAR_INCREMENT 환경변수가 LinearBackoff에 반영."""
        with mock.patch.dict(os.environ, {"BALDUR_BACKOFF_LINEAR_INCREMENT": "5.0"}):
            from baldur.core.backoff import LinearBackoff
            from baldur.settings.backoff import reset_backoff_settings

            reset_backoff_settings()
            backoff = LinearBackoff.from_settings()

            assert backoff.increment == 5.0

    def test_constant_delay_override(self):
        """CONSTANT_DELAY 환경변수가 ConstantBackoff에 반영."""
        with mock.patch.dict(os.environ, {"BALDUR_BACKOFF_CONSTANT_DELAY": "10.0"}):
            from baldur.core.backoff import ConstantBackoff
            from baldur.settings.backoff import reset_backoff_settings

            reset_backoff_settings()
            backoff = ConstantBackoff.from_settings()

            assert backoff.delay == 10.0


class TestMultiModuleEnvOverride:
    """여러 Settings 모듈을 동시에 오버라이드할 때 격리 검증."""

    @pytest.fixture(autouse=True)
    def reset_all_settings(self):
        """Reset all settings before and after each test."""
        from baldur.settings.cleanup import reset_cleanup_settings
        from baldur.settings.jitter import reset_jitter_settings
        from baldur.settings.stress_test import reset_stress_test_settings

        reset_stress_test_settings()
        reset_cleanup_settings()
        reset_jitter_settings()
        yield
        reset_stress_test_settings()
        reset_cleanup_settings()
        reset_jitter_settings()

    def test_multiple_modules_independent(self):
        """각 모듈의 환경변수가 독립적으로 동작하는지 검증."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_STRESS_TEST_DEFAULT_LOCK_TIMEOUT_MS": "7",
                "BALDUR_CLEANUP_ARCHIVE_OLDER_THAN_DAYS": "45",
                "BALDUR_JITTER_MAX_DELAY_SECONDS": "90.0",
            },
        ):
            from baldur.settings.cleanup import (
                get_cleanup_settings,
                reset_cleanup_settings,
            )
            from baldur.settings.jitter import (
                get_jitter_settings,
                reset_jitter_settings,
            )
            from baldur.settings.stress_test import (
                get_stress_test_settings,
                reset_stress_test_settings,
            )

            reset_stress_test_settings()
            reset_cleanup_settings()
            reset_jitter_settings()

            stress_settings = get_stress_test_settings()
            cleanup_settings = get_cleanup_settings()
            jitter_settings = get_jitter_settings()

            assert stress_settings.default_lock_timeout_ms == 7
            assert cleanup_settings.archive_older_than_days == 45
            assert jitter_settings.max_delay_seconds == 90.0

            # 다른 필드는 기본값 유지
            assert stress_settings.max_burst_duration_seconds == 30
            assert cleanup_settings.purge_older_than_days == 90
            assert jitter_settings.enabled is True
