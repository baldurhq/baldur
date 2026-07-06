"""
Unit tests for RunbookSettings.

검증 항목:
- 설계 계약값 (기본값, 범위)
- 환경 변수 오버라이드
- 필드 유효성 검증
- 싱글톤 캐싱/리셋

테스트 대상: baldur.settings.runbook
"""

import os
from unittest import mock

import pytest
from pydantic import ValidationError


class TestRunbookSettingsContract:
    """RunbookSettings 설계 계약값 검증.

    272_RUNBOOK_ARCHITECTURE_OVERVIEW.md §7에 명시된 기본값을 검증한다.
    """

    def test_enabled_default_is_false(self):
        """런북 시스템 기본 비활성 (v1.1 deferred per impl 527)."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.enabled is False

    def test_approval_timer_seconds_default_is_300(self):
        """MEDIUM 위험도 타이머 자동 승인 대기 시간: 300초 (5분). §10 설계 계약."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.approval_timer_seconds == 300

    def test_max_concurrent_runbooks_default_is_3(self):
        """동시 실행 런북 수 제한: 3."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.max_concurrent_runbooks == 3

    def test_step_default_timeout_seconds_default_is_120(self):
        """step 기본 타임아웃: 120초 (2분)."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.step_default_timeout_seconds == 120

    def test_lock_ttl_seconds_default_is_600(self):
        """분산 락 TTL: 600초 (10분)."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.lock_ttl_seconds == 600

    def test_field_count(self):
        """RunbookSettings는 21개 필드로 구성된다 (272: 4개 + 275: 7개 + 276: 5개 + 278: 5개)."""
        from baldur.settings.runbook import RunbookSettings

        assert len(RunbookSettings.model_fields) == 21

    # === 275번 Executor 설정 계약값 ===

    def test_global_timeout_seconds_default_is_1800(self):
        """전체 실행 타임아웃: 1800초 (30분). §16 설계 계약."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.global_timeout_seconds == 1800

    def test_lock_extend_seconds_default_is_300(self):
        """Lock TTL 연장 기본값: 300초. §16 설계 계약."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.lock_extend_seconds == 300

    def test_lock_heartbeat_interval_default_is_60(self):
        """Lock Heartbeat Polling 간격: 60초. §16 설계 계약."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.lock_heartbeat_interval == 60

    def test_idempotency_ttl_hours_default_is_24(self):
        """멱등성 키 TTL: 24시간. §16 설계 계약."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.idempotency_ttl_hours == 24

    def test_context_ttl_seconds_default_is_86400(self):
        """컨텍스트 영속화 TTL: 86400초 (24시간). §16 설계 계약."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.context_ttl_seconds == 86400

    def test_resume_stale_threshold_seconds_default_is_3600(self):
        """Resume stale 임계값: 3600초 (1시간). §16 설계 계약."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.resume_stale_threshold_seconds == 3600

    def test_max_resume_count_default_is_10(self):
        """무한 재개 방지 카운터: 10. §16 설계 계약."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = RunbookSettings()
            assert settings.max_resume_count == 10


class TestRunbookSettingsBehavior:
    """RunbookSettings 동작 검증."""

    def test_env_override_enabled(self):
        """환경 변수로 enabled를 비활성화할 수 있다."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_ENABLED": "false"},
            clear=True,
        ):
            settings = RunbookSettings()
            assert settings.enabled is False

    def test_env_override_approval_timer(self):
        """환경 변수로 타이머 승인 대기 시간을 변경할 수 있다."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_APPROVAL_TIMER_SECONDS": "600"},
            clear=True,
        ):
            settings = RunbookSettings()
            assert settings.approval_timer_seconds == 600

    def test_env_override_max_concurrent_runbooks(self):
        """환경 변수로 동시 실행 런북 수를 변경할 수 있다."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_MAX_CONCURRENT_RUNBOOKS": "5"},
            clear=True,
        ):
            settings = RunbookSettings()
            assert settings.max_concurrent_runbooks == 5

    def test_env_override_step_timeout(self):
        """환경 변수로 step 타임아웃을 변경할 수 있다."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_STEP_DEFAULT_TIMEOUT_SECONDS": "240"},
            clear=True,
        ):
            settings = RunbookSettings()
            assert settings.step_default_timeout_seconds == 240

    def test_env_override_lock_ttl(self):
        """환경 변수로 락 TTL을 변경할 수 있다."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_LOCK_TTL_SECONDS": "1200"},
            clear=True,
        ):
            settings = RunbookSettings()
            assert settings.lock_ttl_seconds == 1200

    def test_approval_timer_below_minimum_rejected(self):
        """타이머 승인 대기 시간이 최소값(30) 미만이면 ValidationError 발생."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_APPROVAL_TIMER_SECONDS": "10"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                RunbookSettings()

    def test_approval_timer_above_maximum_rejected(self):
        """타이머 승인 대기 시간이 최대값(7200) 초과이면 ValidationError 발생."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_APPROVAL_TIMER_SECONDS": "7201"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                RunbookSettings()

    def test_max_concurrent_below_minimum_rejected(self):
        """동시 실행 런북 수가 최소값(1) 미만이면 ValidationError 발생."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_MAX_CONCURRENT_RUNBOOKS": "0"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                RunbookSettings()

    def test_max_concurrent_above_maximum_rejected(self):
        """동시 실행 런북 수가 최대값(20) 초과이면 ValidationError 발생."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_MAX_CONCURRENT_RUNBOOKS": "25"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                RunbookSettings()

    def test_step_timeout_below_minimum_rejected(self):
        """step 타임아웃이 최소값(10) 미만이면 ValidationError 발생."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_STEP_DEFAULT_TIMEOUT_SECONDS": "5"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                RunbookSettings()

    def test_lock_ttl_below_minimum_rejected(self):
        """락 TTL이 최소값(60) 미만이면 ValidationError 발생."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_LOCK_TTL_SECONDS": "30"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                RunbookSettings()

    # === 275번 Executor 설정 경계값 ===

    def test_global_timeout_below_minimum_rejected(self):
        """전체 타임아웃이 최소값(60) 미만이면 ValidationError 발생."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_GLOBAL_TIMEOUT_SECONDS": "30"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                RunbookSettings()

    def test_lock_heartbeat_interval_below_minimum_rejected(self):
        """Heartbeat 간격이 최소값(10) 미만이면 ValidationError 발생."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_LOCK_HEARTBEAT_INTERVAL": "5"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                RunbookSettings()

    def test_max_resume_count_below_minimum_rejected(self):
        """재개 카운터가 최소값(1) 미만이면 ValidationError 발생."""
        from baldur.settings.runbook import RunbookSettings, reset_runbook_settings

        reset_runbook_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_RUNBOOK_MAX_RESUME_COUNT": "0"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                RunbookSettings()


class TestRunbookSettingsSingletonBehavior:
    """RunbookSettings 싱글톤 캐싱/리셋 동작 검증."""

    def test_get_returns_same_instance(self):
        """get_runbook_settings()는 동일 인스턴스를 반환."""
        from baldur.settings.runbook import (
            get_runbook_settings,
            reset_runbook_settings,
        )

        reset_runbook_settings()
        first = get_runbook_settings()
        second = get_runbook_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset 후 get_runbook_settings()는 새 인스턴스를 반환."""
        from baldur.settings.runbook import (
            get_runbook_settings,
            reset_runbook_settings,
        )

        reset_runbook_settings()
        first = get_runbook_settings()
        reset_runbook_settings()
        second = get_runbook_settings()
        assert first is not second

    def test_settings_importable_from_package(self):
        """settings 패키지에서 RunbookSettings를 import할 수 있다."""
        from baldur.settings import (
            RunbookSettings,
            get_runbook_settings,
            reset_runbook_settings,
        )

        assert RunbookSettings is not None
        assert callable(get_runbook_settings)
        assert callable(reset_runbook_settings)
