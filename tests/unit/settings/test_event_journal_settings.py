"""
Unit tests for EventJournalSettings.

검증 항목:
- 설계 계약값 (기본값)
- Pydantic 경계값 제약 (ge/le)
- 환경 변수 오버라이드
- 싱글톤 캐싱/리셋

테스트 대상: baldur.settings.event_journal
"""

import os
from unittest import mock

import pytest
from pydantic import ValidationError

from baldur.settings.event_journal import (
    EventJournalSettings,
    get_event_journal_settings,
    reset_event_journal_settings,
)


class TestEventJournalSettingsContract:
    """EventJournalSettings 설계 계약값 검증."""

    def setup_method(self):
        reset_event_journal_settings()

    def test_enabled_default_is_true(self):
        """Journal 활성화 기본값: True."""
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EventJournalSettings()
            assert settings.enabled is True

    def test_ttl_days_default_is_30(self):
        """Redis TTL 기본값: 30일."""
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EventJournalSettings()
            assert settings.ttl_days == 30

    def test_max_entries_memory_default_is_10000(self):
        """InMemory 최대 엔트리 수 기본값: 10000."""
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EventJournalSettings()
            assert settings.max_entries_memory == 10000

    def test_max_query_limit_default_is_10000(self):
        """query() 최대 반환 건수 기본값: 10000."""
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = EventJournalSettings()
            assert settings.max_query_limit == 10000

    def test_env_prefix_is_baldur_journal(self):
        """환경변수 prefix: BALDUR_EVENT_JOURNAL_."""
        assert (
            EventJournalSettings.model_config["env_prefix"] == "BALDUR_EVENT_JOURNAL_"
        )

    def test_field_count_is_4(self):
        """EventJournalSettings has 4 fields after the `backend` field removal (570 D3)."""
        assert len(EventJournalSettings.model_fields) == 4


class TestEventJournalSettingsBoundaryBehavior:
    """EventJournalSettings 필드 경계값 검증."""

    def test_ttl_days_minimum_boundary_at_7(self):
        """ttl_days 최소 경계: ge=7. 6은 실패, 7은 성공."""
        with pytest.raises(ValidationError):
            EventJournalSettings(ttl_days=6)
        settings = EventJournalSettings(ttl_days=7)
        assert settings.ttl_days == 7

    def test_ttl_days_maximum_boundary_at_365(self):
        """ttl_days 최대 경계: le=365. 365는 성공, 366은 실패."""
        settings = EventJournalSettings(ttl_days=365)
        assert settings.ttl_days == 365
        with pytest.raises(ValidationError):
            EventJournalSettings(ttl_days=366)

    def test_max_entries_memory_minimum_boundary_at_100(self):
        """max_entries_memory 최소 경계: ge=100. 99는 실패, 100은 성공."""
        with pytest.raises(ValidationError):
            EventJournalSettings(max_entries_memory=99)
        settings = EventJournalSettings(max_entries_memory=100)
        assert settings.max_entries_memory == 100

    def test_max_entries_memory_maximum_boundary_at_1000000(self):
        """max_entries_memory 최대 경계: le=1000000. 1000000은 성공, 1000001은 실패."""
        settings = EventJournalSettings(max_entries_memory=1000000)
        assert settings.max_entries_memory == 1000000
        with pytest.raises(ValidationError):
            EventJournalSettings(max_entries_memory=1000001)

    def test_max_query_limit_minimum_boundary_at_100(self):
        """max_query_limit 최소 경계: ge=100. 99는 실패, 100은 성공."""
        with pytest.raises(ValidationError):
            EventJournalSettings(max_query_limit=99)
        settings = EventJournalSettings(max_query_limit=100)
        assert settings.max_query_limit == 100

    def test_max_query_limit_maximum_boundary_at_100000(self):
        """max_query_limit 최대 경계: le=100000. 100000은 성공, 100001은 실패."""
        settings = EventJournalSettings(max_query_limit=100000)
        assert settings.max_query_limit == 100000
        with pytest.raises(ValidationError):
            EventJournalSettings(max_query_limit=100001)


class TestEventJournalSettingsEnvOverrideBehavior:
    """EventJournalSettings 환경변수 오버라이드 검증."""

    def setup_method(self):
        reset_event_journal_settings()

    def test_env_override_ttl_days(self):
        """BALDUR_EVENT_JOURNAL_TTL_DAYS 환경변수로 ttl_days 오버라이드."""
        with mock.patch.dict(
            os.environ, {"BALDUR_EVENT_JOURNAL_TTL_DAYS": "60"}, clear=True
        ):
            settings = EventJournalSettings()
            assert settings.ttl_days == 60

    def test_env_override_enabled_false(self):
        """BALDUR_EVENT_JOURNAL_ENABLED=false 환경변수로 비활성화."""
        with mock.patch.dict(
            os.environ, {"BALDUR_EVENT_JOURNAL_ENABLED": "false"}, clear=True
        ):
            settings = EventJournalSettings()
            assert settings.enabled is False


class TestEventJournalSettingsSingletonBehavior:
    """EventJournalSettings 싱글톤 캐싱/리셋 동작 검증."""

    def setup_method(self):
        reset_event_journal_settings()

    def test_get_returns_same_instance(self):
        """get_event_journal_settings()는 동일 인스턴스를 반환."""
        first = get_event_journal_settings()
        second = get_event_journal_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset 후 새 인스턴스가 생성된다."""
        first = get_event_journal_settings()
        reset_event_journal_settings()
        second = get_event_journal_settings()
        assert first is not second
