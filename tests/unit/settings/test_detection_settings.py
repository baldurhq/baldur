"""
Unit tests for DetectionSettings.

검증 항목:
- 설계 계약값 (기본값, 필드 수)
- 경계값 분석 (ge/le 제약)
- 환경 변수 오버라이드
- 싱글톤 캐싱/리셋 (Root 경유 SSOT)

테스트 대상: baldur.settings.detection
참조: 313_SETTINGS_CONFIGURATION_CONSISTENCY.md §2.2
"""

import os
from unittest import mock

import pytest
from pydantic import ValidationError

# =============================================================================
# 계약 검증: 설계 문서에 명시된 기본값
# =============================================================================


class TestDetectionSettingsContract:
    """DetectionSettings 설계 계약값 검증.

    313 §2.2에 명시된 기본값 및 환경변수 프리픽스를 검증한다.
    """

    def test_anomaly_window_size_default_is_100(self):
        """이상 탐지 슬라이딩 윈도우 크기: 100. 313 §2.2 설계 계약."""
        from baldur.settings.detection import (
            DetectionSettings,
            reset_detection_settings,
        )

        reset_detection_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = DetectionSettings()
            assert settings.anomaly_window_size == 100

    def test_anomaly_zscore_threshold_default_is_3(self):
        """Z-Score 임계값: 3.0 (99.7% 신뢰구간). 313 §2.2 설계 계약."""
        from baldur.settings.detection import (
            DetectionSettings,
            reset_detection_settings,
        )

        reset_detection_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = DetectionSettings()
            assert settings.anomaly_zscore_threshold == 3.0

    def test_anomaly_window_max_age_seconds_default_is_300(self):
        """하이브리드 윈도우 최대 보존 시간: 300초. 313 Q6 결정."""
        from baldur.settings.detection import (
            DetectionSettings,
            reset_detection_settings,
        )

        reset_detection_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = DetectionSettings()
            assert settings.anomaly_window_max_age_seconds == 300.0

    def test_correlation_window_size_default_is_100(self):
        """상관 분석 윈도우 크기: 100. 313 §2.2 설계 계약."""
        from baldur.settings.detection import (
            DetectionSettings,
            reset_detection_settings,
        )

        reset_detection_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = DetectionSettings()
            assert settings.correlation_window_size == 100

    def test_field_count_is_4(self):
        """DetectionSettings는 4개 필드로 구성된다."""
        from baldur.settings.detection import DetectionSettings

        assert len(DetectionSettings.model_fields) == 4

    def test_env_prefix_is_baldur_detection(self):
        """환경변수 프리픽스: BALDUR_DETECTION_."""
        from baldur.settings.detection import DetectionSettings

        assert DetectionSettings.model_config["env_prefix"] == "BALDUR_DETECTION_"


# =============================================================================
# 경계값 분석: ge/le 제약
# =============================================================================


class TestDetectionSettingsBoundaryContract:
    """DetectionSettings 필드 경계값 계약 검증."""

    def test_anomaly_window_size_minimum_boundary(self):
        """anomaly_window_size의 최소 경계: ge=10."""
        from baldur.settings.detection import DetectionSettings

        with pytest.raises(ValidationError):
            DetectionSettings(anomaly_window_size=9)
        settings = DetectionSettings(anomaly_window_size=10)
        assert settings.anomaly_window_size == 10

    def test_anomaly_window_size_maximum_boundary(self):
        """anomaly_window_size의 최대 경계: le=10000."""
        from baldur.settings.detection import DetectionSettings

        settings = DetectionSettings(anomaly_window_size=10000)
        assert settings.anomaly_window_size == 10000
        with pytest.raises(ValidationError):
            DetectionSettings(anomaly_window_size=10001)

    def test_anomaly_zscore_threshold_minimum_boundary(self):
        """anomaly_zscore_threshold의 최소 경계: ge=1.0."""
        from baldur.settings.detection import DetectionSettings

        with pytest.raises(ValidationError):
            DetectionSettings(anomaly_zscore_threshold=0.9)
        settings = DetectionSettings(anomaly_zscore_threshold=1.0)
        assert settings.anomaly_zscore_threshold == 1.0

    def test_anomaly_zscore_threshold_maximum_boundary(self):
        """anomaly_zscore_threshold의 최대 경계: le=10.0."""
        from baldur.settings.detection import DetectionSettings

        settings = DetectionSettings(anomaly_zscore_threshold=10.0)
        assert settings.anomaly_zscore_threshold == 10.0
        with pytest.raises(ValidationError):
            DetectionSettings(anomaly_zscore_threshold=10.1)

    def test_anomaly_window_max_age_seconds_minimum_boundary(self):
        """anomaly_window_max_age_seconds의 최소 경계: ge=10.0."""
        from baldur.settings.detection import DetectionSettings

        with pytest.raises(ValidationError):
            DetectionSettings(anomaly_window_max_age_seconds=9.9)
        settings = DetectionSettings(anomaly_window_max_age_seconds=10.0)
        assert settings.anomaly_window_max_age_seconds == 10.0

    def test_anomaly_window_max_age_seconds_maximum_boundary(self):
        """anomaly_window_max_age_seconds의 최대 경계: le=86400.0."""
        from baldur.settings.detection import DetectionSettings

        settings = DetectionSettings(anomaly_window_max_age_seconds=86400.0)
        assert settings.anomaly_window_max_age_seconds == 86400.0
        with pytest.raises(ValidationError):
            DetectionSettings(anomaly_window_max_age_seconds=86400.1)

    def test_correlation_window_size_minimum_boundary(self):
        """correlation_window_size의 최소 경계: ge=10."""
        from baldur.settings.detection import DetectionSettings

        with pytest.raises(ValidationError):
            DetectionSettings(correlation_window_size=9)
        settings = DetectionSettings(correlation_window_size=10)
        assert settings.correlation_window_size == 10

    def test_correlation_window_size_maximum_boundary(self):
        """correlation_window_size의 최대 경계: le=10000."""
        from baldur.settings.detection import DetectionSettings

        settings = DetectionSettings(correlation_window_size=10000)
        assert settings.correlation_window_size == 10000
        with pytest.raises(ValidationError):
            DetectionSettings(correlation_window_size=10001)


# =============================================================================
# 동작 검증: 환경변수 오버라이드 및 싱글톤
# =============================================================================


class TestDetectionSettingsBehavior:
    """DetectionSettings 동작 검증."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from baldur.settings.detection import reset_detection_settings

        reset_detection_settings()
        yield
        reset_detection_settings()

    def test_env_override_anomaly_window_size(self):
        """BALDUR_DETECTION_ANOMALY_WINDOW_SIZE 환경변수로 오버라이드."""
        from baldur.settings.detection import DetectionSettings

        with mock.patch.dict(
            os.environ, {"BALDUR_DETECTION_ANOMALY_WINDOW_SIZE": "200"}, clear=True
        ):
            settings = DetectionSettings()
            assert settings.anomaly_window_size == 200

    def test_env_override_anomaly_zscore_threshold(self):
        """BALDUR_DETECTION_ANOMALY_ZSCORE_THRESHOLD 환경변수로 오버라이드."""
        from baldur.settings.detection import DetectionSettings

        with mock.patch.dict(
            os.environ,
            {"BALDUR_DETECTION_ANOMALY_ZSCORE_THRESHOLD": "4.5"},
            clear=True,
        ):
            settings = DetectionSettings()
            assert settings.anomaly_zscore_threshold == 4.5

    def test_env_override_correlation_window_size(self):
        """BALDUR_DETECTION_CORRELATION_WINDOW_SIZE 환경변수로 오버라이드."""
        from baldur.settings.detection import DetectionSettings

        with mock.patch.dict(
            os.environ,
            {"BALDUR_DETECTION_CORRELATION_WINDOW_SIZE": "500"},
            clear=True,
        ):
            settings = DetectionSettings()
            assert settings.correlation_window_size == 500

    def test_root_ssot_returns_detection_settings(self):
        """get_detection_settings()는 Root 경유 SSOT로 동작한다."""
        from baldur.settings.detection import (
            DetectionSettings,
            get_detection_settings,
        )
        from baldur.settings.root import reset_config

        reset_config()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = get_detection_settings()
            assert isinstance(settings, DetectionSettings)

    def test_reset_clears_cached_root(self):
        """reset_detection_settings() 후 새 설정이 로드된다."""
        from baldur.settings.detection import (
            DetectionSettings,
            get_detection_settings,
            reset_detection_settings,
        )

        reset_detection_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_DETECTION_ANOMALY_WINDOW_SIZE": "250"},
            clear=True,
        ):
            s1 = get_detection_settings()
            assert s1.anomaly_window_size == 250

        reset_detection_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            s2 = get_detection_settings()
            fresh = DetectionSettings()
            assert s2.anomaly_window_size == fresh.anomaly_window_size
