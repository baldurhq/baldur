"""
Unit tests for HttpClientSettings.webhook_timeout field.

검증 항목:
- 설계 계약값 (기본값 10.0초)
- 경계값 분석 (ge=1.0, le=60.0)
- 환경 변수 오버라이드

테스트 대상: baldur.settings.http_client (webhook_timeout 필드)
참조: 313_SETTINGS_CONFIGURATION_CONSISTENCY.md Q1 결정
"""

import os
from unittest import mock

import pytest
from pydantic import ValidationError

# =============================================================================
# 계약 검증: webhook_timeout 설계 계약값
# =============================================================================


class TestHttpClientWebhookTimeoutContract:
    """HttpClientSettings.webhook_timeout 설계 계약값 검증.

    313 Q1 결정: webhook_timeout은 HttpClientSettings에 속한다 (HTTP 도메인).
    """

    def test_webhook_timeout_default_is_10(self):
        """웹훅 HTTP 호출 타임아웃: 10.0초. 313 Q1 설계 계약."""
        from baldur.settings.http_client import (
            HttpClientSettings,
            reset_http_client_settings,
        )

        reset_http_client_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = HttpClientSettings()
            assert settings.webhook_timeout == 10.0

    def test_webhook_timeout_field_exists(self):
        """webhook_timeout 필드가 HttpClientSettings에 존재한다."""
        from baldur.settings.http_client import HttpClientSettings

        assert "webhook_timeout" in HttpClientSettings.model_fields


# =============================================================================
# 경계값 분석: webhook_timeout ge/le 제약
# =============================================================================


class TestHttpClientWebhookTimeoutBoundaryContract:
    """HttpClientSettings.webhook_timeout 필드 경계값 계약 검증."""

    def test_webhook_timeout_minimum_boundary(self):
        """webhook_timeout의 최소 경계: ge=1.0."""
        from baldur.settings.http_client import HttpClientSettings

        with pytest.raises(ValidationError):
            HttpClientSettings(webhook_timeout=0.9)
        settings = HttpClientSettings(webhook_timeout=1.0)
        assert settings.webhook_timeout == 1.0

    def test_webhook_timeout_maximum_boundary(self):
        """webhook_timeout의 최대 경계: le=60.0."""
        from baldur.settings.http_client import HttpClientSettings

        settings = HttpClientSettings(webhook_timeout=60.0)
        assert settings.webhook_timeout == 60.0
        with pytest.raises(ValidationError):
            HttpClientSettings(webhook_timeout=60.1)


# =============================================================================
# 동작 검증: 환경변수 오버라이드
# =============================================================================


class TestHttpClientWebhookTimeoutBehavior:
    """HttpClientSettings.webhook_timeout 동작 검증."""

    def test_env_override_webhook_timeout(self):
        """BALDUR_HTTP_WEBHOOK_TIMEOUT 환경변수로 오버라이드."""
        from baldur.settings.http_client import HttpClientSettings

        with mock.patch.dict(
            os.environ, {"BALDUR_HTTP_CLIENT_WEBHOOK_TIMEOUT": "20.0"}, clear=True
        ):
            settings = HttpClientSettings()
            assert settings.webhook_timeout == 20.0


# =============================================================================
# 687 D7: webhook retry budget fields
# =============================================================================


class TestHttpClientWebhookRetryContract:
    """HttpClientSettings.webhook_retry_{total,backoff_factor} — 687 D7.

    Externalizes the outbound-webhook retry budget that used to live inline in
    the security-notification RETRY_STRATEGY.
    """

    def test_webhook_retry_total_default_is_2(self):
        """webhook_retry_total default: 2 (mirrors RETRY_STRATEGY.total)."""
        from baldur.settings.http_client import (
            HttpClientSettings,
            reset_http_client_settings,
        )

        reset_http_client_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            assert HttpClientSettings().webhook_retry_total == 2

    def test_webhook_retry_backoff_factor_default_is_half(self):
        """webhook_retry_backoff_factor default: 0.5 (mirrors RETRY_STRATEGY)."""
        from baldur.settings.http_client import (
            HttpClientSettings,
            reset_http_client_settings,
        )

        reset_http_client_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            assert HttpClientSettings().webhook_retry_backoff_factor == 0.5

    def test_webhook_retry_total_minimum_boundary(self):
        """webhook_retry_total ge=0: -1 fails, 0 passes."""
        from baldur.settings.http_client import HttpClientSettings

        with pytest.raises(ValidationError):
            HttpClientSettings(webhook_retry_total=-1)
        assert HttpClientSettings(webhook_retry_total=0).webhook_retry_total == 0

    def test_webhook_retry_total_maximum_boundary(self):
        """webhook_retry_total le=5: 5 passes, 6 fails."""
        from baldur.settings.http_client import HttpClientSettings

        assert HttpClientSettings(webhook_retry_total=5).webhook_retry_total == 5
        with pytest.raises(ValidationError):
            HttpClientSettings(webhook_retry_total=6)

    def test_webhook_retry_backoff_factor_maximum_boundary(self):
        """webhook_retry_backoff_factor le=5.0: 5.0 passes, 5.1 fails."""
        from baldur.settings.http_client import HttpClientSettings

        assert HttpClientSettings(webhook_retry_backoff_factor=5.0)
        with pytest.raises(ValidationError):
            HttpClientSettings(webhook_retry_backoff_factor=5.1)

    def test_webhook_retry_total_env_override(self):
        """BALDUR_HTTP_CLIENT_WEBHOOK_RETRY_TOTAL overrides the default."""
        from baldur.settings.http_client import HttpClientSettings

        with mock.patch.dict(
            os.environ, {"BALDUR_HTTP_CLIENT_WEBHOOK_RETRY_TOTAL": "4"}, clear=True
        ):
            assert HttpClientSettings().webhook_retry_total == 4
