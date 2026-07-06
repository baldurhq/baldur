"""
Tests for Celery Task Settings - 108_HARDCODED_CONFIG_REFACTORING_PART1_CELERY_TASKS.md.

Celery 태스크 데코레이터의 하드코딩된 설정값을 Settings 기반으로 리팩토링한 모듈 테스트:
- CleanupSettings (확장): cleanup_tasks.py 태스크 재시도 설정
- DailyReportSettings (신규): daily_report.py 태스크 재시도 설정
- GovernanceSettings (확장): governance.py 태스크 재시도 설정
- ApplyStrategySettings (확장): config_apply.py 태스크 재시도 설정
- ChaosSettings (확장): chaos_scheduler.py 태스크 재시도 설정
"""

import pytest
from pydantic import ValidationError

# =============================================================================
# CleanupSettings Tests (확장된 Celery Task 재시도 설정)
# =============================================================================


class TestCleanupSettingsCeleryTask:
    """CleanupSettings의 Celery Task 재시도 설정 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.cleanup import reset_cleanup_settings

        reset_cleanup_settings()
        yield
        reset_cleanup_settings()

    def test_default_archive_dlq_task_settings(self):
        """archive_old_dlq_entries_task 기본값 검증."""
        from baldur.settings.cleanup import CleanupSettings

        settings = CleanupSettings()

        # cleanup_tasks.py: max_retries=2, default_retry_delay=300
        assert settings.archive_dlq_max_retries == 2
        assert settings.archive_dlq_retry_delay == 300

    def test_default_expired_config_task_settings(self):
        """cleanup_expired_config_task 기본값 검증."""
        from baldur.settings.cleanup import CleanupSettings

        settings = CleanupSettings()

        # cleanup_tasks.py: max_retries=2, default_retry_delay=300
        assert settings.expired_config_max_retries == 2
        assert settings.expired_config_retry_delay == 300

    def test_default_approval_task_settings(self):
        """expire_approval_requests_task 기본값 검증."""
        from baldur.settings.cleanup import CleanupSettings

        settings = CleanupSettings()

        # cleanup_tasks.py: max_retries=2, default_retry_delay=300
        assert settings.approval_max_retries == 2
        assert settings.approval_retry_delay == 300

    def test_default_purge_dlq_task_settings(self):
        """purge_archived_dlq_entries_task 기본값 검증 (고위험 작업)."""
        from baldur.settings.cleanup import CleanupSettings

        settings = CleanupSettings()

        # cleanup_tasks.py: max_retries=1, default_retry_delay=600
        assert settings.purge_dlq_max_retries == 1
        assert settings.purge_dlq_retry_delay == 600

    def test_env_override_cleanup_task_settings(self, monkeypatch):
        """환경변수로 Cleanup 태스크 설정 오버라이드 검증."""
        from baldur.settings.cleanup import CleanupSettings

        monkeypatch.setenv("BALDUR_CLEANUP_ARCHIVE_DLQ_MAX_RETRIES", "5")
        monkeypatch.setenv("BALDUR_CLEANUP_ARCHIVE_DLQ_RETRY_DELAY", "600")
        monkeypatch.setenv("BALDUR_CLEANUP_PURGE_DLQ_MAX_RETRIES", "2")

        settings = CleanupSettings()

        assert settings.archive_dlq_max_retries == 5
        assert settings.archive_dlq_retry_delay == 600
        assert settings.purge_dlq_max_retries == 2

    def test_validation_purge_max_retries_limit(self):
        """purge_dlq_max_retries 최대값(3) 검증 - 고위험 작업."""
        from baldur.settings.cleanup import CleanupSettings

        # 최대값 허용
        settings = CleanupSettings(purge_dlq_max_retries=3)
        assert settings.purge_dlq_max_retries == 3

        # 최대값 초과 시 ValidationError
        with pytest.raises(ValidationError):
            CleanupSettings(purge_dlq_max_retries=4)


# =============================================================================
# DailyReportSettings Tests (신규)
# =============================================================================


class TestDailyReportSettings:
    """DailyReportSettings 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.daily_report import reset_daily_report_settings

        reset_daily_report_settings()
        yield
        reset_daily_report_settings()

    def test_default_values(self):
        """기본값이 daily_report.py의 Celery 데코레이터 값과 일치하는지 검증."""
        from baldur.settings.daily_report import DailyReportSettings

        settings = DailyReportSettings()

        # daily_report.py: max_retries=2, default_retry_delay=300
        assert settings.max_retries == 2
        assert settings.retry_delay == 300

        # 기본 채널 및 시간
        assert settings.default_channels == ["slack"]
        assert settings.default_hour == 9
        assert settings.default_minute == 0

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.daily_report import DailyReportSettings

        monkeypatch.setenv("BALDUR_DAILY_REPORT_MAX_RETRIES", "5")
        monkeypatch.setenv("BALDUR_DAILY_REPORT_RETRY_DELAY", "600")

        settings = DailyReportSettings()

        assert settings.max_retries == 5
        assert settings.retry_delay == 600

    def test_validation_max_retries_range(self):
        """max_retries 값 범위(0-10) 검증."""
        from baldur.settings.daily_report import DailyReportSettings

        # 범위 내 값은 허용
        settings = DailyReportSettings(max_retries=0)
        assert settings.max_retries == 0

        settings = DailyReportSettings(max_retries=10)
        assert settings.max_retries == 10

        # 범위 초과 값은 ValidationError
        with pytest.raises(ValidationError):
            DailyReportSettings(max_retries=11)

    def test_singleton_pattern(self):
        """싱글톤 패턴 동작 검증."""
        from baldur.settings.daily_report import (
            get_daily_report_settings,
        )

        settings1 = get_daily_report_settings()
        settings2 = get_daily_report_settings()

        assert settings1 is settings2


# =============================================================================
# GovernanceSettings Tests (확장된 Celery Task 재시도 설정)
# =============================================================================


class TestGovernanceSettingsCeleryTask:
    """GovernanceSettings의 Celery Task 재시도 설정 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.governance import reset_governance_settings

        reset_governance_settings()
        yield
        reset_governance_settings()

    def test_default_expiry_check_task_settings(self):
        """check_emergency_mode_expiry_task 기본값 검증."""
        from baldur.settings.governance import GovernanceSettings

        settings = GovernanceSettings()

        # governance.py: max_retries=3, default_retry_delay=60
        assert settings.expiry_check_max_retries == 3
        assert settings.expiry_check_retry_delay == 60

    def test_env_override_governance_task_settings(self, monkeypatch):
        """환경변수로 Governance 태스크 설정 오버라이드 검증."""
        from baldur.settings.governance import GovernanceSettings

        monkeypatch.setenv("BALDUR_GOVERNANCE_EXPIRY_CHECK_MAX_RETRIES", "5")
        monkeypatch.setenv("BALDUR_GOVERNANCE_EXPIRY_CHECK_RETRY_DELAY", "120")

        settings = GovernanceSettings()

        assert settings.expiry_check_max_retries == 5
        assert settings.expiry_check_retry_delay == 120


# =============================================================================
# ApplyStrategySettings Tests (확장된 Celery Task 재시도 설정)
# =============================================================================


class TestApplyStrategySettingsCeleryTask:
    """ApplyStrategySettings의 Celery Task 재시도 설정 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.apply_strategy import reset_apply_strategy_settings

        reset_apply_strategy_settings()
        yield
        reset_apply_strategy_settings()

    def test_default_pending_task_settings(self):
        """apply_pending_config_changes 태스크 기본값 검증."""
        from baldur.settings.apply_strategy import ApplyStrategySettings

        settings = ApplyStrategySettings()

        # config_apply.py: max_retries=3, default_retry_delay=10
        assert settings.pending_max_retries == 3
        assert settings.pending_retry_delay == 10

    def test_default_graceful_task_settings(self):
        """apply_graceful_config_change 태스크 기본값 검증."""
        from baldur.settings.apply_strategy import ApplyStrategySettings

        settings = ApplyStrategySettings()

        # config_apply.py: max_retries=10, default_retry_delay=5
        assert settings.graceful_max_retries == 10
        assert settings.graceful_retry_delay == 5

    def test_default_cleanup_max_age_hours(self):
        """cleanup_expired_config_changes 태스크 기본값 검증."""
        from baldur.settings.apply_strategy import ApplyStrategySettings

        settings = ApplyStrategySettings()

        # config_apply.py: max_age_hours=24
        assert settings.cleanup_max_age_hours == 24

    def test_env_override_apply_task_settings(self, monkeypatch):
        """환경변수로 Apply 태스크 설정 오버라이드 검증."""
        from baldur.settings.apply_strategy import ApplyStrategySettings

        monkeypatch.setenv("BALDUR_APPLY_STRATEGY_PENDING_MAX_RETRIES", "5")
        monkeypatch.setenv("BALDUR_APPLY_STRATEGY_GRACEFUL_MAX_RETRIES", "15")
        monkeypatch.setenv("BALDUR_APPLY_STRATEGY_CLEANUP_MAX_AGE_HOURS", "48")

        settings = ApplyStrategySettings()

        assert settings.pending_max_retries == 5
        assert settings.graceful_max_retries == 15
        assert settings.cleanup_max_age_hours == 48


# =============================================================================
# ChaosSettings Tests (확장된 Celery Task 재시도 설정)
# =============================================================================


class TestChaosSettingsCeleryTask:
    """ChaosSettings의 Celery Task 재시도 설정 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.chaos import reset_chaos_settings

        reset_chaos_settings()
        yield
        reset_chaos_settings()

    def test_default_experiment_task_settings(self):
        """run_scheduled_experiments_task 기본값 검증."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings()

        # chaos_scheduler.py: max_retries=0, soft_time_limit=300, time_limit=360
        assert settings.scheduler_experiment_max_retries == 0
        assert settings.scheduler_experiment_soft_time_limit == 300
        assert settings.scheduler_experiment_time_limit == 360

    def test_default_report_task_settings(self):
        """generate_daily_resilience_report_task 기본값 검증."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings()

        # chaos_scheduler.py: max_retries=3, default_retry_delay=300
        assert settings.scheduler_report_max_retries == 3
        assert settings.scheduler_report_retry_delay == 300

    def test_default_cleanup_and_pending_task_settings(self):
        """cleanup_expired_approvals_task, check_pending_approvals_task 기본값 검증."""
        from baldur.settings.chaos import ChaosSettings

        settings = ChaosSettings()

        # chaos_scheduler.py: max_retries=1 for both
        assert settings.scheduler_cleanup_max_retries == 1
        assert settings.scheduler_pending_check_max_retries == 1

    def test_env_override_chaos_scheduler_settings(self, monkeypatch):
        """환경변수로 Chaos Scheduler 설정 오버라이드 검증."""
        from baldur.settings.chaos import ChaosSettings

        monkeypatch.setenv("BALDUR_CHAOS_SCHEDULER_EXPERIMENT_MAX_RETRIES", "1")
        monkeypatch.setenv("BALDUR_CHAOS_SCHEDULER_EXPERIMENT_SOFT_TIME_LIMIT", "600")
        monkeypatch.setenv("BALDUR_CHAOS_SCHEDULER_REPORT_MAX_RETRIES", "5")

        settings = ChaosSettings()

        assert settings.scheduler_experiment_max_retries == 1
        assert settings.scheduler_experiment_soft_time_limit == 600
        assert settings.scheduler_report_max_retries == 5

    def test_validation_experiment_max_retries_limit(self):
        """scheduler_experiment_max_retries 최대값(3) 검증 - 안전상 낮게 제한."""
        from baldur.settings.chaos import ChaosSettings

        # 최대값 허용
        settings = ChaosSettings(scheduler_experiment_max_retries=3)
        assert settings.scheduler_experiment_max_retries == 3

        # 최대값 초과 시 ValidationError
        with pytest.raises(ValidationError):
            ChaosSettings(scheduler_experiment_max_retries=4)


# =============================================================================
# Integration Test - Settings to Task Decorator
# =============================================================================


class TestSettingsIntegration:
    """Settings 값이 태스크 데코레이터에 올바르게 적용되는지 검증."""

    def test_recovery_tasks_settings_integration(self):
        """RecoveryTasksSettings 값이 recovery_tasks.py에 적용되는지 검증."""
        from baldur.settings.recovery_tasks import get_recovery_tasks_settings

        settings = get_recovery_tasks_settings()

        # 설정값이 존재하고 유효한 범위인지 확인
        assert 0 <= settings.check_trigger_max_retries <= 10
        assert 5 <= settings.check_trigger_retry_delay <= 600
        assert 0 <= settings.execute_step_max_retries <= 10
        assert 5 <= settings.execute_step_retry_delay <= 600

    def test_cleanup_settings_integration(self):
        """CleanupSettings 값이 cleanup_tasks.py에 적용되는지 검증."""
        from baldur.settings.cleanup import get_cleanup_settings

        settings = get_cleanup_settings()

        # 설정값이 존재하고 유효한 범위인지 확인
        assert 0 <= settings.archive_dlq_max_retries <= 10
        assert 10 <= settings.archive_dlq_retry_delay <= 1800

    def test_daily_report_settings_integration(self):
        """DailyReportSettings 값이 daily_report.py에 적용되는지 검증."""
        from baldur.settings.daily_report import get_daily_report_settings

        settings = get_daily_report_settings()

        # 설정값이 존재하고 유효한 범위인지 확인
        assert 0 <= settings.max_retries <= 10
        assert 30 <= settings.retry_delay <= 1800

    def test_governance_settings_integration(self):
        """GovernanceSettings 값이 governance.py에 적용되는지 검증."""
        from baldur.settings.governance import get_governance_settings

        settings = get_governance_settings()

        # 설정값이 존재하고 유효한 범위인지 확인
        assert 0 <= settings.expiry_check_max_retries <= 10
        assert 10 <= settings.expiry_check_retry_delay <= 600

    def test_apply_strategy_settings_integration(self):
        """ApplyStrategySettings 값이 config_apply.py에 적용되는지 검증."""
        from baldur.settings.apply_strategy import get_apply_strategy_settings

        settings = get_apply_strategy_settings()

        # 설정값이 존재하고 유효한 범위인지 확인
        assert 0 <= settings.pending_max_retries <= 10
        assert 1 <= settings.pending_retry_delay <= 300
        assert 0 <= settings.graceful_max_retries <= 20

    def test_chaos_settings_integration(self):
        """ChaosSettings 값이 chaos_scheduler.py에 적용되는지 검증."""
        from baldur.settings.chaos import get_chaos_settings

        settings = get_chaos_settings()

        # 설정값이 존재하고 유효한 범위인지 확인
        assert 0 <= settings.scheduler_experiment_max_retries <= 3
        assert 60 <= settings.scheduler_experiment_soft_time_limit <= 1800
        assert 120 <= settings.scheduler_experiment_time_limit <= 2400
