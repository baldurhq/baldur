"""
Unit tests for KafkaProducerSettings.

검증 항목:
- 설계 계약값 (기본값, 필드 수)
- 경계값 분석 (ge/le 제약)
- 환경 변수 오버라이드
- 싱글톤 캐싱/리셋 (Root 경유 SSOT)

테스트 대상: baldur.settings.kafka_producer
참조: 313 리뷰 Finding #2/#3 — Kafka 전용 타임아웃 설정
"""

import os
from unittest import mock

import pytest
from pydantic import ValidationError

# =============================================================================
# 계약 검증: 설계 기본값
# =============================================================================


class TestKafkaProducerSettingsContract:
    """KafkaProducerSettings 설계 계약값 검증."""

    def test_request_timeout_ms_default_is_10000(self):
        """Kafka protocol request timeout: 10000ms. 313 설계 계약."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        with mock.patch.dict(os.environ, {}, clear=True):
            settings = KafkaProducerSettings()
            assert settings.request_timeout_ms == 10000

    def test_send_timeout_default_is_10(self):
        """producer.send().get() timeout: 10.0초. 313 설계 계약."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        with mock.patch.dict(os.environ, {}, clear=True):
            settings = KafkaProducerSettings()
            assert settings.send_timeout == 10.0

    def test_shutdown_timeout_default_is_5(self):
        """producer.flush()/close() timeout: 5.0초. 313 설계 계약."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        with mock.patch.dict(os.environ, {}, clear=True):
            settings = KafkaProducerSettings()
            assert settings.shutdown_timeout == 5.0

    def test_field_count_is_3(self):
        """KafkaProducerSettings는 3개 필드로 구성된다."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        assert len(KafkaProducerSettings.model_fields) == 3

    def test_env_prefix_is_baldur_kafka_producer(self):
        """환경변수 프리픽스: BALDUR_KAFKA_PRODUCER_."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        assert (
            KafkaProducerSettings.model_config["env_prefix"] == "BALDUR_KAFKA_PRODUCER_"
        )


# =============================================================================
# 경계값 분석: ge/le 제약
# =============================================================================


class TestKafkaProducerSettingsBoundaryContract:
    """KafkaProducerSettings 필드 경계값 계약 검증."""

    def test_request_timeout_ms_minimum_boundary(self):
        """request_timeout_ms의 최소 경계: ge=1000."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        with pytest.raises(ValidationError):
            KafkaProducerSettings(request_timeout_ms=999)
        settings = KafkaProducerSettings(request_timeout_ms=1000)
        assert settings.request_timeout_ms == 1000

    def test_request_timeout_ms_maximum_boundary(self):
        """request_timeout_ms의 최대 경계: le=120000."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        settings = KafkaProducerSettings(request_timeout_ms=120000)
        assert settings.request_timeout_ms == 120000
        with pytest.raises(ValidationError):
            KafkaProducerSettings(request_timeout_ms=120001)

    def test_send_timeout_minimum_boundary(self):
        """send_timeout의 최소 경계: ge=1.0."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        with pytest.raises(ValidationError):
            KafkaProducerSettings(send_timeout=0.9)
        settings = KafkaProducerSettings(send_timeout=1.0)
        assert settings.send_timeout == 1.0

    def test_send_timeout_maximum_boundary(self):
        """send_timeout의 최대 경계: le=120.0."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        settings = KafkaProducerSettings(send_timeout=120.0)
        assert settings.send_timeout == 120.0
        with pytest.raises(ValidationError):
            KafkaProducerSettings(send_timeout=120.1)

    def test_shutdown_timeout_minimum_boundary(self):
        """shutdown_timeout의 최소 경계: ge=1.0."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        with pytest.raises(ValidationError):
            KafkaProducerSettings(shutdown_timeout=0.9)
        settings = KafkaProducerSettings(shutdown_timeout=1.0)
        assert settings.shutdown_timeout == 1.0

    def test_shutdown_timeout_maximum_boundary(self):
        """shutdown_timeout의 최대 경계: le=60.0."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        settings = KafkaProducerSettings(shutdown_timeout=60.0)
        assert settings.shutdown_timeout == 60.0
        with pytest.raises(ValidationError):
            KafkaProducerSettings(shutdown_timeout=60.1)


# =============================================================================
# 동작 검증: 환경변수 오버라이드 및 싱글톤
# =============================================================================


class TestKafkaProducerSettingsBehavior:
    """KafkaProducerSettings 동작 검증."""

    def test_env_override_request_timeout_ms(self):
        """BALDUR_KAFKA_PRODUCER_REQUEST_TIMEOUT_MS 환경변수로 오버라이드."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        with mock.patch.dict(
            os.environ,
            {"BALDUR_KAFKA_PRODUCER_REQUEST_TIMEOUT_MS": "20000"},
            clear=True,
        ):
            settings = KafkaProducerSettings()
            assert settings.request_timeout_ms == 20000

    def test_env_override_send_timeout(self):
        """BALDUR_KAFKA_PRODUCER_SEND_TIMEOUT 환경변수로 오버라이드."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        with mock.patch.dict(
            os.environ,
            {"BALDUR_KAFKA_PRODUCER_SEND_TIMEOUT": "15.0"},
            clear=True,
        ):
            settings = KafkaProducerSettings()
            assert settings.send_timeout == 15.0

    def test_env_override_shutdown_timeout(self):
        """BALDUR_KAFKA_PRODUCER_SHUTDOWN_TIMEOUT 환경변수로 오버라이드."""
        from baldur.settings.kafka_producer import KafkaProducerSettings

        with mock.patch.dict(
            os.environ,
            {"BALDUR_KAFKA_PRODUCER_SHUTDOWN_TIMEOUT": "8.0"},
            clear=True,
        ):
            settings = KafkaProducerSettings()
            assert settings.shutdown_timeout == 8.0

    def test_root_ssot_returns_kafka_producer_settings(self):
        """get_kafka_producer_settings()는 Root 경유 SSOT로 동작한다."""
        from baldur.settings.kafka_producer import (
            KafkaProducerSettings,
            get_kafka_producer_settings,
        )
        from baldur.settings.root import reset_config

        reset_config()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = get_kafka_producer_settings()
            assert isinstance(settings, KafkaProducerSettings)

    def test_reset_clears_cached_root(self):
        """reset_kafka_producer_settings() 후 새 설정이 로드된다."""
        from baldur.settings.kafka_producer import (
            get_kafka_producer_settings,
            reset_kafka_producer_settings,
        )

        reset_kafka_producer_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_KAFKA_PRODUCER_REQUEST_TIMEOUT_MS": "30000"},
            clear=True,
        ):
            s1 = get_kafka_producer_settings()
            assert s1.request_timeout_ms == 30000

        reset_kafka_producer_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            s2 = get_kafka_producer_settings()
            assert s2.request_timeout_ms == 10000
