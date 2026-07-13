"""
Chaos Engineering 및 알림 Settings 테스트.

Chaos Engineering:
- ChaosBlastRadiusSettings: Chaos 실험 폭발 반경 제한 설정
- ChaosExperimentSettings: Chaos 실험 기본 설정

데이터 보호 및 알림:
- CorruptionShieldSettings: 데이터 손상 방지 설정
- NotificationChannelSettings: 알림 채널 설정

회로 차단기 고급:
- CascadeRetentionSettings: 캐스케이드 보존 설정
- CircuitBreakerAdvancedSettings: 서킷브레이커 고급 설정
"""

import pytest
from pydantic import ValidationError


class TestChaosBlastRadiusSettings:
    """Tests for ChaosBlastRadiusSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.chaos_blast_radius import (
            reset_chaos_blast_radius_settings,
        )

        reset_chaos_blast_radius_settings()
        yield
        reset_chaos_blast_radius_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.chaos_blast_radius import ChaosBlastRadiusSettings

        settings = ChaosBlastRadiusSettings()

        assert settings.instance_max_concurrent == 5
        assert settings.service_max_concurrent == 2
        assert settings.region_max_concurrent == 1
        assert settings.instance_auto_approve is True

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.chaos_blast_radius import ChaosBlastRadiusSettings

        monkeypatch.setenv("BALDUR_CHAOS_BLAST_RADIUS_INSTANCE_MAX_CONCURRENT", "10")

        settings = ChaosBlastRadiusSettings()

        assert settings.instance_max_concurrent == 10

    def test_validation_concurrent_range(self):
        """concurrent 범위 검증."""
        from baldur.settings.chaos_blast_radius import ChaosBlastRadiusSettings

        with pytest.raises(ValidationError):
            ChaosBlastRadiusSettings(instance_max_concurrent=0)  # < 1

        with pytest.raises(ValidationError):
            ChaosBlastRadiusSettings(service_max_concurrent=20)  # > 10

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.chaos_blast_radius import (
            get_chaos_blast_radius_settings,
        )

        settings1 = get_chaos_blast_radius_settings()
        settings2 = get_chaos_blast_radius_settings()

        assert settings1 is settings2


class TestChaosExperimentSettings:
    """Tests for ChaosExperimentSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.chaos_experiment import (
            reset_chaos_experiment_settings,
        )

        reset_chaos_experiment_settings()
        yield
        reset_chaos_experiment_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.chaos_experiment import ChaosExperimentSettings

        settings = ChaosExperimentSettings()

        assert settings.max_duration_seconds == 3600
        assert settings.default_duration_seconds == 300
        assert settings.default_ttl_seconds == 600
        assert settings.grace_period_seconds == 300

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.chaos_experiment import ChaosExperimentSettings

        monkeypatch.setenv("BALDUR_CHAOS_EXPERIMENT_MAX_DURATION_SECONDS", "7200")

        settings = ChaosExperimentSettings()

        assert settings.max_duration_seconds == 7200

    def test_validation_duration_range(self):
        """duration 범위 검증."""
        from baldur.settings.chaos_experiment import ChaosExperimentSettings

        with pytest.raises(ValidationError):
            ChaosExperimentSettings(max_duration_seconds=30)  # < 60

        with pytest.raises(ValidationError):
            ChaosExperimentSettings(max_duration_seconds=100000)  # > 86400

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.chaos_experiment import get_chaos_experiment_settings

        settings1 = get_chaos_experiment_settings()
        settings2 = get_chaos_experiment_settings()

        assert settings1 is settings2


class TestCorruptionShieldSettings:
    """Tests for CorruptionShieldSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.corruption_shield import (
            reset_corruption_shield_settings,
        )

        reset_corruption_shield_settings()
        yield
        reset_corruption_shield_settings()

    def test_default_values(self):
        """기본값 검증 (v1.1 deferred per impl 527 — layer enabled flags default False)."""
        from baldur.settings.corruption_shield import CorruptionShieldSettings

        settings = CorruptionShieldSettings()

        assert settings.l1_enabled is False
        assert settings.l2_enabled is False
        assert settings.l3_enabled is False
        assert "amount" in settings.required_fields
        assert settings.max_string_length == 1000

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.corruption_shield import CorruptionShieldSettings

        monkeypatch.setenv("BALDUR_CORRUPTION_SHIELD_L3_ENABLED", "false")

        settings = CorruptionShieldSettings()

        assert settings.l3_enabled is False

    def test_validation_max_string_range(self):
        """max_string_length 범위 검증."""
        from baldur.settings.corruption_shield import CorruptionShieldSettings

        with pytest.raises(ValidationError):
            CorruptionShieldSettings(max_string_length=50)  # < 100

        with pytest.raises(ValidationError):
            CorruptionShieldSettings(max_string_length=20000)  # > 10000

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.corruption_shield import (
            get_corruption_shield_settings,
        )

        settings1 = get_corruption_shield_settings()
        settings2 = get_corruption_shield_settings()

        assert settings1 is settings2


class TestNotificationChannelSettings:
    """Tests for NotificationChannelSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.notification_channel import (
            reset_notification_channel_settings,
        )

        reset_notification_channel_settings()
        yield
        reset_notification_channel_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.notification_channel import (
            NotificationChannelSettings,
        )

        settings = NotificationChannelSettings()

        assert settings.rate_limit_per_minute == 60
        assert settings.rate_limit_per_hour == 300
        assert settings.max_retry == 3
        assert settings.retry_delay_seconds == 30

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.notification_channel import (
            NotificationChannelSettings,
        )

        monkeypatch.setenv("BALDUR_NOTIFICATION_CHANNEL_RATE_LIMIT_PER_MINUTE", "100")

        settings = NotificationChannelSettings()

        assert settings.rate_limit_per_minute == 100

    def test_validation_rate_limit_range(self):
        """rate_limit 범위 검증."""
        from baldur.settings.notification_channel import (
            NotificationChannelSettings,
        )

        with pytest.raises(ValidationError):
            NotificationChannelSettings(rate_limit_per_minute=0)  # < 1

        with pytest.raises(ValidationError):
            NotificationChannelSettings(rate_limit_per_hour=10000)  # > 5000

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.notification_channel import (
            get_notification_channel_settings,
        )

        settings1 = get_notification_channel_settings()
        settings2 = get_notification_channel_settings()

        assert settings1 is settings2


class TestCascadeRetentionSettings:
    """Tests for CascadeRetentionSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.cascade_retention import (
            reset_cascade_retention_settings,
        )

        reset_cascade_retention_settings()
        yield
        reset_cascade_retention_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.cascade_retention import CascadeRetentionSettings

        settings = CascadeRetentionSettings()

        assert settings.hot_retention_days == 7
        assert settings.hot_max_count == 10000
        assert settings.warm_retention_days == 90

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.cascade_retention import CascadeRetentionSettings

        monkeypatch.setenv("BALDUR_CASCADE_RETENTION_HOT_RETENTION_DAYS", "14")

        settings = CascadeRetentionSettings()

        assert settings.hot_retention_days == 14

    def test_validation_retention_days_range(self):
        """retention_days 범위 검증."""
        from baldur.settings.cascade_retention import CascadeRetentionSettings

        with pytest.raises(ValidationError):
            CascadeRetentionSettings(hot_retention_days=0)  # < 1

        with pytest.raises(ValidationError):
            CascadeRetentionSettings(warm_retention_days=400)  # > 365

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.cascade_retention import (
            get_cascade_retention_settings,
        )

        settings1 = get_cascade_retention_settings()
        settings2 = get_cascade_retention_settings()

        assert settings1 is settings2


class TestCircuitBreakerAdvancedSettings:
    """Tests for CircuitBreakerAdvancedSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.circuit_breaker_advanced import (
            reset_circuit_breaker_advanced_settings,
        )

        reset_circuit_breaker_advanced_settings()
        yield
        reset_circuit_breaker_advanced_settings()

    def test_default_values(self):
        """기본값 검증 (v1.1 deferred per impl 527 — all advanced enabled flags False)."""
        from baldur.settings.circuit_breaker_advanced import (
            CircuitBreakerAdvancedSettings,
        )

        settings = CircuitBreakerAdvancedSettings()

        assert settings.enabled is False
        assert settings.load_shedding_enabled is False
        assert settings.load_shedding_trigger_threshold == 30.0

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.circuit_breaker_advanced import (
            CircuitBreakerAdvancedSettings,
        )

        monkeypatch.setenv("BALDUR_CB_ADVANCED_LOAD_SHEDDING_ENABLED", "false")

        settings = CircuitBreakerAdvancedSettings()

        assert settings.load_shedding_enabled is False

    def test_validation_threshold_range(self):
        """threshold 범위 검증."""
        from baldur.settings.circuit_breaker_advanced import (
            CircuitBreakerAdvancedSettings,
        )

        with pytest.raises(ValidationError):
            CircuitBreakerAdvancedSettings(load_shedding_trigger_threshold=-10.0)  # < 0

        with pytest.raises(ValidationError):
            CircuitBreakerAdvancedSettings(
                load_shedding_trigger_threshold=150.0
            )  # > 100

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.circuit_breaker_advanced import (
            get_circuit_breaker_advanced_settings,
        )

        settings1 = get_circuit_breaker_advanced_settings()
        settings2 = get_circuit_breaker_advanced_settings()

        assert settings1 is settings2
