"""
Meta-Watchdog 설정 테스트.

MetaWatchdogSettings 설정 관리 테스트.
"""

import os
from unittest import mock

import pytest
from pydantic import ValidationError

from baldur.meta.config import (
    MetaWatchdogSettings,
    get_meta_watchdog_settings,
    reset_meta_watchdog_settings,
)


class TestMetaWatchdogSettings:
    """MetaWatchdogSettings 테스트."""

    def test_default_values(self):
        """기본값 확인.

        enabled / escalation_enabled 기본값은 True (impl 558 D3 — detect+escalate
        slice promoted to PRO v1.0). recovery_enabled / self_cb_enabled stay
        False (recovery deferred to slice B/C, self-CB a separate NON-GOAL).
        """
        settings = MetaWatchdogSettings()

        assert settings.enabled is True
        assert settings.probe_interval_seconds == 30.0
        assert settings.probe_timeout_seconds == 10.0
        assert settings.stuck_threshold_seconds == 300.0
        assert settings.dlq_stuck_threshold_entries == 1000
        assert settings.self_cb_enabled is False
        assert settings.self_cb_failure_threshold == 5
        assert settings.self_cb_recovery_timeout_seconds == 60.0
        assert settings.escalation_enabled is True
        assert settings.recovery_enabled is False
        assert settings.escalation_cooldown_seconds == 3600.0
        assert settings.pagerduty_routing_key is None
        assert settings.slack_webhook_url is None
        assert settings.dry_run_mode is False
        assert settings.maintenance_components == []

    def test_env_override(self):
        """환경변수 오버라이드 확인."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_META_WATCHDOG_ENABLED": "false",
                "BALDUR_META_WATCHDOG_PROBE_INTERVAL_SECONDS": "60",
                "BALDUR_META_WATCHDOG_DRY_RUN_MODE": "true",
            },
        ):
            settings = MetaWatchdogSettings()

            assert settings.enabled is False
            assert settings.probe_interval_seconds == 60.0
            assert settings.dry_run_mode is True

    def test_pagerduty_severity_options(self):
        """PagerDuty 심각도 옵션 확인."""
        for severity in ["critical", "error", "warning", "info"]:
            with mock.patch.dict(
                os.environ,
                {"BALDUR_META_WATCHDOG_PAGERDUTY_SEVERITY": severity},
            ):
                settings = MetaWatchdogSettings()
                assert settings.pagerduty_severity == severity

    def test_maintenance_components_list(self):
        """유지보수 컴포넌트 목록 확인."""
        with mock.patch.dict(
            os.environ,
            {"BALDUR_META_WATCHDOG_MAINTENANCE_COMPONENTS": '["redis", "dlq"]'},
        ):
            settings = MetaWatchdogSettings()
            assert settings.maintenance_components == ["redis", "dlq"]


class TestMetaWatchdogSettingsEmergencyStuckContract:
    """638 D8 — emergency_stuck_threshold_seconds field contract.

    Emergency recovery/hold is a tens-of-minutes phenomenon, so the field
    defaults to 1800s (30 min), distinct from the generic stuck_threshold_seconds
    (300s), and floors at 60s (ge=60.0).
    """

    def test_default_is_1800_seconds(self):
        """Default emergency stuck threshold is 1800.0 seconds (30 minutes)."""
        assert MetaWatchdogSettings().emergency_stuck_threshold_seconds == 1800.0

    def test_lower_bound_60_is_accepted(self):
        """60.0 is exactly at the ge=60.0 floor → accepted (boundary)."""
        settings = MetaWatchdogSettings(emergency_stuck_threshold_seconds=60.0)

        assert settings.emergency_stuck_threshold_seconds == 60.0

    def test_below_lower_bound_is_rejected(self):
        """59.9 is just below the ge=60.0 floor → rejected (boundary)."""
        with pytest.raises(ValidationError):
            MetaWatchdogSettings(emergency_stuck_threshold_seconds=59.9)

    def test_env_var_binding(self):
        """The field binds to BALDUR_META_WATCHDOG_EMERGENCY_STUCK_THRESHOLD_SECONDS."""
        with mock.patch.dict(
            os.environ,
            {"BALDUR_META_WATCHDOG_EMERGENCY_STUCK_THRESHOLD_SECONDS": "3600"},
        ):
            settings = MetaWatchdogSettings()

            assert settings.emergency_stuck_threshold_seconds == 3600.0


class TestSettingsSingleton:
    """설정 싱글톤 테스트."""

    def setup_method(self):
        """테스트 전 캐시 리셋."""
        reset_meta_watchdog_settings()

    def teardown_method(self):
        """테스트 후 캐시 리셋."""
        reset_meta_watchdog_settings()

    def test_singleton_returns_same_instance(self):
        """싱글톤이 동일 인스턴스 반환."""
        settings1 = get_meta_watchdog_settings()
        settings2 = get_meta_watchdog_settings()

        assert settings1 is settings2

    def test_reset_clears_cache(self):
        """리셋이 캐시 초기화."""
        settings1 = get_meta_watchdog_settings()
        reset_meta_watchdog_settings()
        settings2 = get_meta_watchdog_settings()

        # 새 인스턴스 생성 확인
        assert settings1 is not settings2
