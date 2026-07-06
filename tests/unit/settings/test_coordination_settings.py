"""
Tests for Coordination Settings Modules.

104_HARDCODED_CONFIG_COORDINATION_REFACTORING.md Step 1 에서 생성/확장된 Settings 모듈 테스트:
- RecoveryTasksSettings: 복구 태스크별 Celery 재시도 설정
- RecoveryCoordinatorSettings: RecoveryCoordinator 복구 단계 설정
- CriticalWorkerSettings 확장: 환경별 Worker Pool 설정 (DeploymentEnvironment)
"""

import pytest
from pydantic import ValidationError

# =============================================================================
# RecoveryTasksSettings Tests
# =============================================================================


class TestRecoveryTasksSettings:
    """Tests for RecoveryTasksSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.recovery_tasks import reset_recovery_tasks_settings

        reset_recovery_tasks_settings()
        yield
        reset_recovery_tasks_settings()

    def test_default_values(self):
        """기본값이 recovery_tasks.py의 Celery 데코레이터 값과 일치하는지 검증."""
        from baldur.settings.recovery_tasks import RecoveryTasksSettings

        settings = RecoveryTasksSettings()

        # check_recovery_trigger 태스크: max_retries=3, default_retry_delay=60
        assert settings.check_trigger_max_retries == 3
        assert settings.check_trigger_retry_delay == 60

        # execute_recovery_step 태스크: max_retries=3, default_retry_delay=30
        assert settings.execute_step_max_retries == 3
        assert settings.execute_step_retry_delay == 30

        # monitor_active_recovery 태스크: max_retries=3, default_retry_delay=30
        assert settings.monitor_recovery_max_retries == 3
        assert settings.monitor_recovery_retry_delay == 30

        # cleanup_stale_sessions 태스크: max_retries=2, default_retry_delay=15
        assert settings.cleanup_stale_max_retries == 2
        assert settings.cleanup_stale_retry_delay == 15

        # run_health_checks 태스크: max_retries=1
        assert settings.health_check_max_retries == 1
        assert settings.health_check_retry_delay == 60

    def test_default_interval_values(self):
        """태스크 실행 간격 기본값 검증."""
        from baldur.settings.recovery_tasks import RecoveryTasksSettings

        settings = RecoveryTasksSettings()

        # recovery_tasks.py의 상수와 일치
        assert settings.trigger_check_interval == 60  # DEFAULT_TRIGGER_CHECK_INTERVAL
        assert settings.health_monitor_interval == 30  # DEFAULT_HEALTH_MONITOR_INTERVAL
        assert settings.stale_check_interval == 10  # DEFAULT_STALE_CHECK_INTERVAL

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.recovery_tasks import RecoveryTasksSettings

        monkeypatch.setenv("BALDUR_RECOVERY_TASKS_CHECK_TRIGGER_MAX_RETRIES", "5")
        monkeypatch.setenv("BALDUR_RECOVERY_TASKS_EXECUTE_STEP_RETRY_DELAY", "45")
        monkeypatch.setenv("BALDUR_RECOVERY_TASKS_TRIGGER_CHECK_INTERVAL", "120")

        settings = RecoveryTasksSettings()

        assert settings.check_trigger_max_retries == 5
        assert settings.execute_step_retry_delay == 45
        assert settings.trigger_check_interval == 120

    def test_validation_max_retries_range(self):
        """max_retries 값 범위(0-10) 검증."""
        from baldur.settings.recovery_tasks import RecoveryTasksSettings

        # 범위 내 값은 허용
        settings = RecoveryTasksSettings(check_trigger_max_retries=0)
        assert settings.check_trigger_max_retries == 0

        settings = RecoveryTasksSettings(check_trigger_max_retries=10)
        assert settings.check_trigger_max_retries == 10

        # 범위 초과 값은 ValidationError
        with pytest.raises(ValidationError):
            RecoveryTasksSettings(check_trigger_max_retries=11)

    def test_validation_retry_delay_range(self):
        """retry_delay 값 범위 검증."""
        from baldur.settings.recovery_tasks import RecoveryTasksSettings

        # 최소값 미만은 ValidationError
        with pytest.raises(ValidationError):
            RecoveryTasksSettings(check_trigger_retry_delay=4)  # min=5

        # 최대값 초과는 ValidationError
        with pytest.raises(ValidationError):
            RecoveryTasksSettings(check_trigger_retry_delay=601)  # max=600

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.recovery_tasks import get_recovery_tasks_settings

        settings1 = get_recovery_tasks_settings()
        settings2 = get_recovery_tasks_settings()

        assert settings1 is settings2


# =============================================================================
# RecoveryCoordinatorSettings Tests
# =============================================================================


class TestRecoveryCoordinatorSettings:
    """Tests for RecoveryCoordinatorSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.recovery_coordinator import (
            reset_recovery_coordinator_settings,
        )

        reset_recovery_coordinator_settings()
        yield
        reset_recovery_coordinator_settings()

    def test_default_values_level3(self):
        """LEVEL_3 기본값이 recovery_coordinator.py의 DEFAULT_RECOVERY_STEPS와 일치하는지 검증."""
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        settings = RecoveryCoordinatorSettings()

        # LEVEL_3 RecoveryStep 기본값
        assert settings.level3_budget_reset_wait_after == 0
        assert settings.level3_health_check_wait_after == 0
        assert settings.level3_health_check_duration_minutes == 5
        assert settings.level3_health_check_success_threshold == 0.95
        assert settings.level3_health_check_error_rate_threshold == 0.1
        assert settings.level3_canary_resume_wait_after == 60
        assert settings.level3_governance_normal_wait_after == 300  # 5분 안정화

    def test_default_values_level2(self):
        """LEVEL_2 기본값 검증."""
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        settings = RecoveryCoordinatorSettings()

        # LEVEL_2 RecoveryStep 기본값 (recovery_coordinator.py에서)
        assert settings.level2_health_check_duration_minutes == 3
        assert settings.level2_health_check_success_threshold == 0.95
        assert settings.level2_health_check_error_rate_threshold == 0.15
        assert settings.level2_canary_resume_wait_after == 30

    def test_default_values_level1(self):
        """LEVEL_1 기본값 검증."""
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        settings = RecoveryCoordinatorSettings()

        # LEVEL_1 RecoveryStep 기본값
        assert settings.level1_health_check_duration_minutes == 2
        assert settings.level1_health_check_success_threshold == 0.90
        assert settings.level1_health_check_error_rate_threshold == 0.2

    def test_default_stability_check_values(self):
        """안정성 검사 기본값 검증."""
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        settings = RecoveryCoordinatorSettings()

        # recovery_coordinator.py L696-697의 기본값
        assert settings.stability_check_duration_minutes == 10
        assert settings.stability_check_error_rate_threshold == 0.1
        assert settings.stability_check_success_rate_threshold == 0.95

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        monkeypatch.setenv(
            "BALDUR_RECOVERY_COORDINATOR_LEVEL3_HEALTH_CHECK_DURATION_MINUTES", "10"
        )
        monkeypatch.setenv(
            "BALDUR_RECOVERY_COORDINATOR_LEVEL3_CANARY_RESUME_WAIT_AFTER", "120"
        )
        monkeypatch.setenv(
            "BALDUR_RECOVERY_COORDINATOR_STABILITY_CHECK_DURATION_MINUTES", "15"
        )

        settings = RecoveryCoordinatorSettings()

        assert settings.level3_health_check_duration_minutes == 10
        assert settings.level3_canary_resume_wait_after == 120
        assert settings.stability_check_duration_minutes == 15

    def test_validation_success_threshold_range(self):
        """success_threshold 값 범위(0.8-1.0) 검증."""
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        # 범위 내 값은 허용
        settings = RecoveryCoordinatorSettings(
            level3_health_check_success_threshold=0.8
        )
        assert settings.level3_health_check_success_threshold == 0.8

        # 범위 미만은 ValidationError
        with pytest.raises(ValidationError):
            RecoveryCoordinatorSettings(level3_health_check_success_threshold=0.79)

    def test_validation_error_rate_threshold_range(self):
        """error_rate_threshold 값 범위 검증."""
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        # 범위 내 값은 허용
        settings = RecoveryCoordinatorSettings(
            level3_health_check_error_rate_threshold=0.01
        )
        assert settings.level3_health_check_error_rate_threshold == 0.01

        # 범위 초과는 ValidationError
        with pytest.raises(ValidationError):
            RecoveryCoordinatorSettings(level3_health_check_error_rate_threshold=0.31)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.recovery_coordinator import (
            get_recovery_coordinator_settings,
        )

        settings1 = get_recovery_coordinator_settings()
        settings2 = get_recovery_coordinator_settings()

        assert settings1 is settings2


# =============================================================================
# CriticalWorkerSettings Extended Tests (Worker Pool)
# =============================================================================


class TestCriticalWorkerSettingsWorkerPool:
    """Tests for CriticalWorkerSettings Worker Pool extensions."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.critical_worker import reset_critical_worker_settings

        reset_critical_worker_settings()
        yield
        reset_critical_worker_settings()

    def test_deployment_environment_enum(self):
        """DeploymentEnvironment Enum 값 검증."""
        from baldur.settings.critical_worker import DeploymentEnvironment

        assert DeploymentEnvironment.MINIMAL.value == "MINIMAL"
        assert DeploymentEnvironment.STANDARD.value == "STANDARD"
        assert DeploymentEnvironment.HIGH_AVAILABILITY.value == "HIGH_AVAILABILITY"
        assert DeploymentEnvironment.BURST.value == "BURST"
        assert DeploymentEnvironment.ENTERPRISE.value == "ENTERPRISE"

    def test_default_deployment_env(self):
        """기본 배포 환경이 STANDARD인지 검증."""
        from baldur.settings.critical_worker import (
            CriticalWorkerSettings,
            DeploymentEnvironment,
        )

        settings = CriticalWorkerSettings()

        assert settings.deployment_env == DeploymentEnvironment.STANDARD

    def test_pool_minimal_defaults(self):
        """MINIMAL 환경 Worker Pool 기본값 검증."""
        from baldur.settings.critical_worker import CriticalWorkerSettings

        settings = CriticalWorkerSettings()

        # critical_worker.py의 MINIMAL Pool 설정과 일치
        assert settings.pool_minimal_worker_count == 2
        assert settings.pool_minimal_concurrency == 2
        assert settings.pool_minimal_prefetch_multiplier == 1

    def test_pool_standard_defaults(self):
        """STANDARD 환경 Worker Pool 기본값 검증."""
        from baldur.settings.critical_worker import CriticalWorkerSettings

        settings = CriticalWorkerSettings()

        # critical_worker.py의 STANDARD Pool 설정과 일치
        assert settings.pool_standard_worker_count == 4
        assert settings.pool_standard_concurrency == 4
        assert settings.pool_standard_prefetch_multiplier == 2

    def test_pool_high_availability_defaults(self):
        """HIGH_AVAILABILITY 환경 Worker Pool 기본값 검증."""
        from baldur.settings.critical_worker import CriticalWorkerSettings

        settings = CriticalWorkerSettings()

        assert settings.pool_high_availability_worker_count == 4
        assert settings.pool_high_availability_concurrency == 4
        assert settings.pool_high_availability_prefetch_multiplier == 2

    def test_pool_burst_defaults(self):
        """BURST 환경 Worker Pool 기본값 검증."""
        from baldur.settings.critical_worker import CriticalWorkerSettings

        settings = CriticalWorkerSettings()

        assert settings.pool_burst_worker_count == 2
        assert settings.pool_burst_concurrency == 4
        assert settings.pool_burst_prefetch_multiplier == 4

    def test_pool_enterprise_defaults(self):
        """ENTERPRISE 환경 Worker Pool 기본값 검증."""
        from baldur.settings.critical_worker import CriticalWorkerSettings

        settings = CriticalWorkerSettings()

        assert settings.pool_enterprise_worker_count == 8
        assert settings.pool_enterprise_concurrency == 8
        assert settings.pool_enterprise_prefetch_multiplier == 4

    def test_get_pool_config_for_env_minimal(self):
        """get_pool_config_for_env() MINIMAL 환경 반환값 검증."""
        from baldur.settings.critical_worker import (
            CriticalWorkerSettings,
            DeploymentEnvironment,
        )

        settings = CriticalWorkerSettings()
        config = settings.get_pool_config_for_env(DeploymentEnvironment.MINIMAL)

        assert config["worker_count"] == 2
        assert config["concurrency"] == 2
        assert config["prefetch_multiplier"] == 1

    def test_get_pool_config_for_env_enterprise(self):
        """get_pool_config_for_env() ENTERPRISE 환경 반환값 검증."""
        from baldur.settings.critical_worker import (
            CriticalWorkerSettings,
            DeploymentEnvironment,
        )

        settings = CriticalWorkerSettings()
        config = settings.get_pool_config_for_env(DeploymentEnvironment.ENTERPRISE)

        assert config["worker_count"] == 8
        assert config["concurrency"] == 8
        assert config["prefetch_multiplier"] == 4

    def test_get_pool_config_for_env_default(self):
        """get_pool_config_for_env() 기본값(None) 사용 시 deployment_env 반환."""
        from baldur.settings.critical_worker import (
            CriticalWorkerSettings,
            DeploymentEnvironment,
        )

        settings = CriticalWorkerSettings(deployment_env=DeploymentEnvironment.BURST)
        config = settings.get_pool_config_for_env()  # None -> self.deployment_env

        assert config["worker_count"] == 2  # BURST
        assert config["concurrency"] == 4  # BURST
        assert config["prefetch_multiplier"] == 4  # BURST

    def test_env_override_deployment_env(self, monkeypatch):
        """환경변수로 deployment_env를 오버라이드할 수 있는지 검증."""
        from baldur.settings.critical_worker import (
            CriticalWorkerSettings,
            DeploymentEnvironment,
        )

        monkeypatch.setenv("BALDUR_CRITICAL_WORKER_DEPLOYMENT_ENV", "ENTERPRISE")

        settings = CriticalWorkerSettings()

        assert settings.deployment_env == DeploymentEnvironment.ENTERPRISE

    def test_env_override_pool_settings(self, monkeypatch):
        """환경변수로 Pool 설정을 오버라이드할 수 있는지 검증."""
        from baldur.settings.critical_worker import CriticalWorkerSettings

        monkeypatch.setenv("BALDUR_CRITICAL_WORKER_POOL_MINIMAL_WORKER_COUNT", "3")
        monkeypatch.setenv("BALDUR_CRITICAL_WORKER_POOL_ENTERPRISE_CONCURRENCY", "16")

        settings = CriticalWorkerSettings()

        assert settings.pool_minimal_worker_count == 3
        assert settings.pool_enterprise_concurrency == 16


# =============================================================================
# Settings Module Export Tests
# =============================================================================


class TestSettingsModuleExports:
    """Test that new settings are properly exported from __init__.py."""

    def test_recovery_tasks_settings_export(self):
        """RecoveryTasksSettings가 정상적으로 export되는지 검증."""
        from baldur.settings import (
            RecoveryTasksSettings,
            get_recovery_tasks_settings,
            reset_recovery_tasks_settings,
        )

        assert RecoveryTasksSettings is not None
        assert callable(get_recovery_tasks_settings)
        assert callable(reset_recovery_tasks_settings)

    def test_recovery_coordinator_settings_export(self):
        """RecoveryCoordinatorSettings가 정상적으로 export되는지 검증."""
        from baldur.settings import (
            RecoveryCoordinatorSettings,
            get_recovery_coordinator_settings,
            reset_recovery_coordinator_settings,
        )

        assert RecoveryCoordinatorSettings is not None
        assert callable(get_recovery_coordinator_settings)
        assert callable(reset_recovery_coordinator_settings)

    def test_deployment_environment_export(self):
        """DeploymentEnvironment Enum이 정상적으로 export되는지 검증."""
        from baldur.settings import DeploymentEnvironment

        assert DeploymentEnvironment is not None
        assert DeploymentEnvironment.STANDARD.value == "STANDARD"
