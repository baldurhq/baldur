"""
ScaleSettings 및 EventBufferSettings 단위 테스트.

대기업 환경 스케일 설정 및 이벤트 버퍼 설정 테스트.
"""

import os
from unittest.mock import patch

import pytest

from baldur.settings.event_buffer import (
    EventBufferSettings,
    get_event_buffer_settings,
    reset_event_buffer_settings,
)
from baldur.settings.scale import (
    PROFILE_DEFAULTS,
    ScaleProfile,
    ScaleSettings,
    get_scale_settings,
    reset_scale_settings,
)


class TestScaleSettings:
    """ScaleSettings 테스트."""

    def setup_method(self):
        """각 테스트 전 싱글톤 초기화."""
        reset_scale_settings()

    def teardown_method(self):
        """각 테스트 후 싱글톤 정리."""
        reset_scale_settings()

    def test_default_profile_is_development(self):
        """기본 프로파일은 development."""
        settings = ScaleSettings()
        assert settings.profile == ScaleProfile.DEVELOPMENT

    def test_development_profile_defaults(self):
        """development 프로파일 기본값 확인."""
        settings = ScaleSettings()

        assert settings.profile == ScaleProfile.DEVELOPMENT
        assert settings.effective_max_events_per_request == 100
        assert settings.effective_max_events_per_second == 1000
        assert settings.effective_ring_buffer_capacity == 10000
        assert settings.effective_batch_size == 10
        assert settings.effective_flush_interval == 5.0

    def test_small_business_profile_defaults(self):
        """small 프로파일 기본값 확인."""
        with patch.dict(os.environ, {"BALDUR_SCALE_PROFILE": "small"}, clear=False):
            settings = ScaleSettings()

        assert settings.profile == ScaleProfile.SMALL_BUSINESS
        assert settings.effective_max_events_per_request == 1000
        assert settings.effective_max_events_per_second == 10000
        assert settings.effective_ring_buffer_capacity == 100000
        assert settings.effective_batch_size == 100
        assert settings.effective_flush_interval == 3.0

    def test_medium_business_profile_defaults(self):
        """medium 프로파일 기본값 확인."""
        with patch.dict(os.environ, {"BALDUR_SCALE_PROFILE": "medium"}, clear=False):
            settings = ScaleSettings()

        assert settings.profile == ScaleProfile.MEDIUM_BUSINESS
        assert settings.effective_max_events_per_request == 10000
        assert settings.effective_max_events_per_second == 50000
        assert settings.effective_ring_buffer_capacity == 500000
        assert settings.effective_batch_size == 500
        assert settings.effective_flush_interval == 2.0

    def test_enterprise_profile_defaults(self):
        """enterprise 프로파일 기본값 확인."""
        with patch.dict(
            os.environ, {"BALDUR_SCALE_PROFILE": "enterprise"}, clear=False
        ):
            settings = ScaleSettings()

        assert settings.profile == ScaleProfile.ENTERPRISE
        assert settings.effective_max_events_per_request == 50000
        assert settings.effective_max_events_per_second == 200000
        assert settings.effective_ring_buffer_capacity == 1000000
        assert settings.effective_batch_size == 1000
        assert settings.effective_flush_interval == 1.0

    def test_high_throughput_profile_defaults(self):
        """high 프로파일 기본값 확인."""
        with patch.dict(os.environ, {"BALDUR_SCALE_PROFILE": "high"}, clear=False):
            settings = ScaleSettings()

        assert settings.profile == ScaleProfile.HIGH_THROUGHPUT
        assert settings.effective_max_events_per_request == 100000
        assert settings.effective_max_events_per_second == 1000000
        assert settings.effective_ring_buffer_capacity == 5000000
        assert settings.effective_batch_size == 5000
        assert settings.effective_flush_interval == 0.5

    def test_individual_override_max_events_per_second(self):
        """개별 값 오버라이드 - max_events_per_second."""
        env = {
            "BALDUR_SCALE_PROFILE": "enterprise",
            "BALDUR_SCALE_MAX_EVENTS_PER_SECOND": "500000",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = ScaleSettings()

        # 오버라이드된 값
        assert settings.effective_max_events_per_second == 500000
        # 프로파일 기본값 유지
        assert settings.effective_max_events_per_request == 50000

    def test_individual_override_max_events_per_request(self):
        """개별 값 오버라이드 - max_events_per_request."""
        env = {
            "BALDUR_SCALE_PROFILE": "development",
            "BALDUR_SCALE_MAX_EVENTS_PER_REQUEST": "5000",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = ScaleSettings()

        # 오버라이드된 값
        assert settings.effective_max_events_per_request == 5000
        # 프로파일 기본값 유지
        assert settings.effective_max_events_per_second == 1000

    def test_individual_override_ring_buffer_capacity(self):
        """개별 값 오버라이드 - ring_buffer_capacity."""
        env = {
            "BALDUR_SCALE_PROFILE": "small",
            "BALDUR_SCALE_RING_BUFFER_CAPACITY": "250000",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = ScaleSettings()

        assert settings.effective_ring_buffer_capacity == 250000
        # 프로파일 기본값 유지
        assert settings.effective_batch_size == 100

    def test_individual_override_batch_size(self):
        """개별 값 오버라이드 - batch_size."""
        env = {
            "BALDUR_SCALE_PROFILE": "medium",
            "BALDUR_SCALE_BATCH_SIZE": "1000",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = ScaleSettings()

        assert settings.effective_batch_size == 1000
        assert settings.effective_max_events_per_second == 50000

    def test_individual_override_flush_interval(self):
        """개별 값 오버라이드 - flush_interval_seconds."""
        env = {
            "BALDUR_SCALE_PROFILE": "enterprise",
            "BALDUR_SCALE_FLUSH_INTERVAL_SECONDS": "0.3",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = ScaleSettings()

        assert settings.effective_flush_interval == 0.3
        assert settings.effective_batch_size == 1000

    def test_singleton_get_scale_settings(self):
        """get_scale_settings 싱글톤 테스트."""
        settings1 = get_scale_settings()
        settings2 = get_scale_settings()

        assert settings1 is settings2

    def test_reset_scale_settings(self):
        """reset_scale_settings 테스트."""
        settings1 = get_scale_settings()
        reset_scale_settings()
        settings2 = get_scale_settings()

        assert settings1 is not settings2

    def test_profile_defaults_dictionary_structure(self):
        """PROFILE_DEFAULTS 딕셔너리 구조 확인."""
        for profile in ScaleProfile:
            assert profile in PROFILE_DEFAULTS
            defaults = PROFILE_DEFAULTS[profile]
            assert "max_events_per_request" in defaults
            assert "max_events_per_second" in defaults
            assert "ring_buffer_capacity" in defaults
            assert "batch_size" in defaults
            assert "flush_interval" in defaults


class TestEventBufferSettings:
    """EventBufferSettings 테스트."""

    def setup_method(self):
        """각 테스트 전 싱글톤 초기화."""
        reset_event_buffer_settings()

    def teardown_method(self):
        """각 테스트 후 싱글톤 정리."""
        reset_event_buffer_settings()

    def test_default_values(self):
        """기본값 확인."""
        settings = EventBufferSettings()

        assert settings.max_events_per_request == 1000
        assert settings.warning_threshold == 0.8
        assert settings.overflow_strategy == "drop_oldest"

    def test_max_events_per_request_limit_100000(self):
        """max_events_per_request 최대값 100,000 테스트."""
        env = {"BALDUR_EVENT_BUFFER_MAX_EVENTS_PER_REQUEST": "100000"}
        with patch.dict(os.environ, env, clear=False):
            settings = EventBufferSettings()

        assert settings.max_events_per_request == 100000

    def test_max_events_per_request_exceeds_limit(self):
        """max_events_per_request 100,000 초과 시 유효성 검사 실패."""
        env = {"BALDUR_EVENT_BUFFER_MAX_EVENTS_PER_REQUEST": "200000"}
        with patch.dict(os.environ, env, clear=False):
            with pytest.raises(Exception):  # ValidationError
                EventBufferSettings()

    def test_overflow_strategy_drop_oldest(self):
        """overflow_strategy drop_oldest 테스트."""
        env = {"BALDUR_EVENT_BUFFER_OVERFLOW_STRATEGY": "drop_oldest"}
        with patch.dict(os.environ, env, clear=False):
            settings = EventBufferSettings()

        assert settings.overflow_strategy == "drop_oldest"

    def test_overflow_strategy_drop_newest(self):
        """overflow_strategy drop_newest 테스트."""
        env = {"BALDUR_EVENT_BUFFER_OVERFLOW_STRATEGY": "drop_newest"}
        with patch.dict(os.environ, env, clear=False):
            settings = EventBufferSettings()

        assert settings.overflow_strategy == "drop_newest"

    def test_overflow_strategy_block(self):
        """overflow_strategy block 테스트."""
        env = {"BALDUR_EVENT_BUFFER_OVERFLOW_STRATEGY": "block"}
        with patch.dict(os.environ, env, clear=False):
            settings = EventBufferSettings()

        assert settings.overflow_strategy == "block"

    def test_warning_threshold_custom(self):
        """warning_threshold 커스텀 값 테스트."""
        env = {"BALDUR_EVENT_BUFFER_WARNING_THRESHOLD": "0.9"}
        with patch.dict(os.environ, env, clear=False):
            settings = EventBufferSettings()

        assert settings.warning_threshold == 0.9

    def test_singleton_get_event_buffer_settings(self):
        """get_event_buffer_settings 싱글톤 테스트."""
        settings1 = get_event_buffer_settings()
        settings2 = get_event_buffer_settings()

        assert settings1 is settings2

    def test_reset_event_buffer_settings(self):
        """reset_event_buffer_settings 테스트."""
        settings1 = get_event_buffer_settings()
        reset_event_buffer_settings()
        settings2 = get_event_buffer_settings()

        assert settings1 is not settings2


class TestCascadeRetentionSettingsUpdated:
    """CascadeRetentionSettings 제한 증가 테스트."""

    def test_max_events_per_second_limit_1000000(self):
        """max_events_per_second 최대값 1,000,000 테스트."""
        from baldur.settings.cascade_retention import (
            CascadeRetentionSettings,
            reset_cascade_retention_settings,
        )

        reset_cascade_retention_settings()

        env = {"BALDUR_CASCADE_RETENTION_MAX_EVENTS_PER_SECOND": "1000000"}
        with patch.dict(os.environ, env, clear=False):
            settings = CascadeRetentionSettings()

        assert settings.max_events_per_second == 1000000

    def test_max_events_per_second_default_10000(self):
        """max_events_per_second 기본값 10,000 테스트."""
        from baldur.settings.cascade_retention import (
            CascadeRetentionSettings,
            reset_cascade_retention_settings,
        )

        reset_cascade_retention_settings()
        settings = CascadeRetentionSettings()

        assert settings.max_events_per_second == 10000


class TestBatchSettingsUpdated:
    """BatchSettings 제한 증가 테스트."""

    def test_logger_batch_size_limit_10000(self):
        """logger_batch_size 최대값 10,000 테스트."""
        from baldur.settings.batch import (
            BatchSettings,
            reset_batch_settings,
        )

        reset_batch_settings()

        env = {"BALDUR_BATCH_LOGGER_BATCH_SIZE": "10000"}
        with patch.dict(os.environ, env, clear=False):
            settings = BatchSettings()

        assert settings.logger_batch_size == 10000

    def test_logger_batch_size_default_100(self):
        """logger_batch_size 기본값 100 테스트."""
        from baldur.settings.batch import (
            BatchSettings,
            reset_batch_settings,
        )

        reset_batch_settings()
        settings = BatchSettings()

        assert settings.logger_batch_size == 100

    def test_flush_interval_min_0_1(self):
        """flush_interval 최소값 0.1초 테스트."""
        from baldur.settings.batch import (
            BatchSettings,
            reset_batch_settings,
        )

        reset_batch_settings()

        env = {"BALDUR_BATCH_FLUSH_INTERVAL": "0.1"}
        with patch.dict(os.environ, env, clear=False):
            settings = BatchSettings()

        assert settings.flush_interval == 0.1
