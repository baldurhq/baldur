"""
복원력(Resilience) Settings 테스트.

시스템 안정성 및 복원력 관련 설정들:
- ErrorBudgetPropagationSettings: 에러 버짓 전파 설정
- AntiFlappingSettings: 플래핑 방지 설정
- DistributedLockSettings: 분산 락 설정
- CriticalWorkerSettings: 중요 워커 설정
"""

import pytest
from pydantic import ValidationError


class TestErrorBudgetPropagationSettings:
    """Tests for ErrorBudgetPropagationSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.error_budget_propagation import (
            reset_error_budget_propagation_settings,
        )

        reset_error_budget_propagation_settings()
        yield
        reset_error_budget_propagation_settings()

    def test_default_values(self):
        """기본값 검증.

        enabled 기본값은 False (impl 527, v1.1 deferred).
        """
        from baldur.settings.error_budget_propagation import (
            ErrorBudgetPropagationSettings,
        )

        settings = ErrorBudgetPropagationSettings()

        assert settings.decay_per_hop == 0.5
        assert settings.max_hops == 3
        assert settings.base_multiplier == 5.0
        assert settings.min_multiplier == 1.0
        assert settings.enabled is False

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.error_budget_propagation import (
            ErrorBudgetPropagationSettings,
        )

        monkeypatch.setenv("BALDUR_ERROR_BUDGET_PROPAGATION_MAX_HOPS", "5")

        settings = ErrorBudgetPropagationSettings()

        assert settings.max_hops == 5

    def test_validation_decay_range(self):
        """decay_per_hop 범위 (0.1-1.0) 검증."""
        from baldur.settings.error_budget_propagation import (
            ErrorBudgetPropagationSettings,
        )

        with pytest.raises(ValidationError):
            ErrorBudgetPropagationSettings(decay_per_hop=0.05)  # < 0.1

        with pytest.raises(ValidationError):
            ErrorBudgetPropagationSettings(decay_per_hop=1.5)  # > 1.0

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.error_budget_propagation import (
            get_error_budget_propagation_settings,
        )

        settings1 = get_error_budget_propagation_settings()
        settings2 = get_error_budget_propagation_settings()

        assert settings1 is settings2


class TestAntiFlappingSettings:
    """Tests for AntiFlappingSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.anti_flapping import reset_anti_flapping_settings

        reset_anti_flapping_settings()
        yield
        reset_anti_flapping_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.anti_flapping import AntiFlappingSettings

        settings = AntiFlappingSettings()

        assert settings.level_cooldown_seconds == 300
        assert settings.cooldown_after_recovery_seconds == 600
        assert settings.min_stable_duration_before_recovery_seconds == 600
        assert settings.max_level_transitions_per_hour == 3

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.anti_flapping import AntiFlappingSettings

        monkeypatch.setenv("BALDUR_ANTI_FLAPPING_LEVEL_COOLDOWN_SECONDS", "600")

        settings = AntiFlappingSettings()

        assert settings.level_cooldown_seconds == 600

    def test_validation_cooldown_range(self):
        """level_cooldown_seconds 범위 (30-3600) 검증."""
        from baldur.settings.anti_flapping import AntiFlappingSettings

        with pytest.raises(ValidationError):
            AntiFlappingSettings(level_cooldown_seconds=10)  # < 30

        with pytest.raises(ValidationError):
            AntiFlappingSettings(level_cooldown_seconds=5000)  # > 3600

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.anti_flapping import get_anti_flapping_settings

        settings1 = get_anti_flapping_settings()
        settings2 = get_anti_flapping_settings()

        assert settings1 is settings2


class TestDistributedLockSettings:
    """Tests for DistributedLockSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.distributed_lock import (
            reset_distributed_lock_settings,
        )

        reset_distributed_lock_settings()
        yield
        reset_distributed_lock_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.distributed_lock import DistributedLockSettings

        settings = DistributedLockSettings()

        assert settings.timeout_minutes == 30
        assert settings.retry_interval_seconds == 0.1
        assert settings.max_retry_attempts == 100
        assert settings.extend_interval_seconds == 60

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.distributed_lock import DistributedLockSettings

        monkeypatch.setenv("BALDUR_DISTRIBUTED_LOCK_TIMEOUT_MINUTES", "60")

        settings = DistributedLockSettings()

        assert settings.timeout_minutes == 60

    def test_validation_timeout_range(self):
        """timeout_minutes 범위 (1-120) 검증."""
        from baldur.settings.distributed_lock import DistributedLockSettings

        with pytest.raises(ValidationError):
            DistributedLockSettings(timeout_minutes=0)  # < 1

        with pytest.raises(ValidationError):
            DistributedLockSettings(timeout_minutes=200)  # > 120

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.distributed_lock import get_distributed_lock_settings

        settings1 = get_distributed_lock_settings()
        settings2 = get_distributed_lock_settings()

        assert settings1 is settings2


class TestCriticalWorkerSettings:
    """Tests for CriticalWorkerSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.critical_worker import reset_critical_worker_settings

        reset_critical_worker_settings()
        yield
        reset_critical_worker_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.critical_worker import CriticalWorkerSettings

        settings = CriticalWorkerSettings()

        assert settings.critical_queue_name == "baldur.critical"
        assert settings.high_priority_queue_name == "baldur.high"
        assert settings.default_queue_name == "baldur.default"
        assert settings.recovery_queue_name == "baldur.recovery"

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.critical_worker import CriticalWorkerSettings

        monkeypatch.setenv(
            "BALDUR_CRITICAL_WORKER_CRITICAL_QUEUE_NAME", "custom.critical"
        )

        settings = CriticalWorkerSettings()

        assert settings.critical_queue_name == "custom.critical"

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.critical_worker import get_critical_worker_settings

        settings1 = get_critical_worker_settings()
        settings2 = get_critical_worker_settings()

        assert settings1 is settings2
