"""
Tests for Regional Emergency Settings Integration.

tracker.py, cascade_detector.py가 settings 모듈의 값을 올바르게 로드하는지 검증.

테스트 범위:
- tracker._get_emergency_expiry_hours(): settings.expiry_hours 로드
- tracker._get_cache_ttl_seconds(): settings.cache_ttl_seconds 로드
- cascade_detector._get_escalation_threshold(): settings.escalation_threshold 로드
- cascade_detector._get_cascade_window_minutes(): settings.cascade_window_minutes 로드
- 환경변수 변경 시 settings 값 반영

Source:
    services/regional_emergency/tracker.py
    services/regional_emergency/cascade_detector.py
    settings/regional_emergency.py
"""

from datetime import UTC
from unittest.mock import MagicMock

import pytest


class TestTrackerSettingsIntegration:
    """tracker.py의 settings 연동 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.services.regional_emergency.tracker import (
            reset_namespaced_emergency_tracker,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        reset_regional_emergency_settings()
        reset_namespaced_emergency_tracker()
        yield
        reset_regional_emergency_settings()
        reset_namespaced_emergency_tracker()

    def test_get_emergency_expiry_hours_loads_from_settings(self):
        """_get_emergency_expiry_hours()가 settings.expiry_hours를 로드."""
        from baldur.services.regional_emergency.tracker import (
            _get_emergency_expiry_hours,
        )

        result = _get_emergency_expiry_hours()

        # settings/regional_emergency.py 기본값: 8
        assert result == 8

    def test_get_cache_ttl_seconds_loads_from_settings(self):
        """_get_cache_ttl_seconds()가 settings.cache_ttl_seconds를 로드."""
        from baldur.services.regional_emergency.tracker import (
            _get_cache_ttl_seconds,
        )

        result = _get_cache_ttl_seconds()

        # settings/regional_emergency.py 기본값: 30.0
        assert result == 30.0

    def test_expiry_hours_env_override_reflected(self, monkeypatch):
        """환경변수 BALDUR_REGIONAL_EMERGENCY_EXPIRY_HOURS 오버라이드 반영."""
        from baldur.services.regional_emergency.tracker import (
            _get_emergency_expiry_hours,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_EXPIRY_HOURS", "24")
        reset_regional_emergency_settings()

        result = _get_emergency_expiry_hours()

        assert result == 24

    def test_cache_ttl_env_override_reflected(self, monkeypatch):
        """환경변수 BALDUR_REGIONAL_EMERGENCY_CACHE_TTL_SECONDS 오버라이드 반영."""
        from baldur.services.regional_emergency.tracker import (
            _get_cache_ttl_seconds,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_CACHE_TTL_SECONDS", "60.0")
        reset_regional_emergency_settings()

        result = _get_cache_ttl_seconds()

        assert result == 60.0

    def test_activate_emergency_uses_settings_expiry(self, monkeypatch):
        """activate_emergency()가 settings.expiry_hours를 기본값으로 사용."""
        from datetime import datetime, timedelta

        from baldur.models.emergency import EmergencyLevel
        from baldur.services.regional_emergency.tracker import (
            NamespacedEmergencyTracker,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        # 환경변수로 만료 시간 설정
        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_EXPIRY_HOURS", "16")
        reset_regional_emergency_settings()

        mock_backend = MagicMock()
        mock_backend.get.return_value = None
        tracker = NamespacedEmergencyTracker(backend=mock_backend)

        state = tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_3,
            activated_by="test",
            reason="Test",
            namespace="seoul",
            # expiry_hours 미지정 시 settings에서 로드
        )

        # 16시간 후 만료 확인 (오차 10초 허용)
        expected_expiry = datetime.now(UTC) + timedelta(hours=16)
        assert abs((state.expires_at - expected_expiry).total_seconds()) < 10

    def test_cache_uses_settings_ttl(self, monkeypatch):
        """_load_state()가 settings.cache_ttl_seconds를 사용."""
        import time

        from baldur.services.regional_emergency.tracker import (
            NamespacedEmergencyTracker,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        # 캐시 TTL을 1초로 설정
        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_CACHE_TTL_SECONDS", "1.0")
        reset_regional_emergency_settings()

        mock_backend = MagicMock()
        mock_backend.get.return_value = {
            "namespace": "seoul",
            "emergency_level": 0,
            "governance_mode": "NORMAL",
            "scope": "regional",
        }
        tracker = NamespacedEmergencyTracker(backend=mock_backend)

        # 첫 번째 호출 (캐시 생성)
        tracker.get_state("seoul")
        first_call_count = mock_backend.get.call_count

        # 캐시 만료 전 호출 (캐시 히트)
        tracker.get_state("seoul")
        assert mock_backend.get.call_count == first_call_count

        # 캐시 만료 후 호출 (backend 재호출)
        time.sleep(1.1)
        tracker.get_state("seoul")
        assert mock_backend.get.call_count > first_call_count


class TestCascadeDetectorSettingsIntegration:
    """cascade_detector.py의 settings 연동 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.services.regional_emergency.cascade_detector import (
            reset_cascade_detector,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        reset_regional_emergency_settings()
        reset_cascade_detector()
        yield
        reset_regional_emergency_settings()
        reset_cascade_detector()

    def test_get_escalation_threshold_loads_from_settings(self):
        """_get_escalation_threshold()가 settings.escalation_threshold를 로드."""
        from baldur.services.regional_emergency.cascade_detector import (
            _get_escalation_threshold,
        )

        result = _get_escalation_threshold()

        # settings/regional_emergency.py 기본값: 2
        assert result == 2

    def test_get_cascade_window_minutes_loads_from_settings(self):
        """_get_cascade_window_minutes()가 settings.cascade_window_minutes를 로드."""
        from baldur.services.regional_emergency.cascade_detector import (
            _get_cascade_window_minutes,
        )

        result = _get_cascade_window_minutes()

        # settings/regional_emergency.py 기본값: 30
        assert result == 30

    def test_escalation_threshold_env_override_reflected(self, monkeypatch):
        """환경변수 BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD 오버라이드 반영."""
        from baldur.services.regional_emergency.cascade_detector import (
            _get_escalation_threshold,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD", "5")
        reset_regional_emergency_settings()

        result = _get_escalation_threshold()

        assert result == 5

    def test_cascade_window_env_override_reflected(self, monkeypatch):
        """환경변수 BALDUR_REGIONAL_EMERGENCY_CASCADE_WINDOW_MINUTES 오버라이드 반영."""
        from baldur.services.regional_emergency.cascade_detector import (
            _get_cascade_window_minutes,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_CASCADE_WINDOW_MINUTES", "60")
        reset_regional_emergency_settings()

        result = _get_cascade_window_minutes()

        assert result == 60

    def test_detector_init_uses_settings_threshold(self, monkeypatch):
        """RegionalCascadeDetector 초기화 시 settings.escalation_threshold 사용."""
        from baldur.services.regional_emergency.cascade_detector import (
            RegionalCascadeDetector,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD", "4")
        reset_regional_emergency_settings()

        mock_tracker = MagicMock()
        # escalation_threshold 미지정 시 settings에서 로드
        detector = RegionalCascadeDetector(tracker=mock_tracker)

        assert detector._threshold == 4

    def test_detector_init_uses_settings_window_minutes(self, monkeypatch):
        """RegionalCascadeDetector 초기화 시 settings.cascade_window_minutes 사용."""
        from baldur.services.regional_emergency.cascade_detector import (
            RegionalCascadeDetector,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_CASCADE_WINDOW_MINUTES", "45")
        reset_regional_emergency_settings()

        mock_tracker = MagicMock()
        # cascade_window_minutes 미지정 시 settings에서 로드
        detector = RegionalCascadeDetector(tracker=mock_tracker)

        assert detector._window_minutes == 45

    def test_detector_explicit_values_override_settings(self, monkeypatch):
        """생성자에 명시적 값 전달 시 settings보다 우선."""
        from baldur.services.regional_emergency.cascade_detector import (
            RegionalCascadeDetector,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD", "4")
        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_CASCADE_WINDOW_MINUTES", "45")
        reset_regional_emergency_settings()

        mock_tracker = MagicMock()
        # 명시적 값 전달
        detector = RegionalCascadeDetector(
            tracker=mock_tracker,
            escalation_threshold=7,
            cascade_window_minutes=90,
        )

        # 명시적 값이 settings보다 우선
        assert detector._threshold == 7
        assert detector._window_minutes == 90

    def test_check_cascade_uses_settings_threshold(self, monkeypatch):
        """check_cascade_condition()이 settings.escalation_threshold 사용."""
        from baldur.models.emergency import EmergencyLevel, ScopedEmergencyState
        from baldur.services.regional_emergency.cascade_detector import (
            RegionalCascadeDetector,
        )
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        # 임계값을 3으로 설정
        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD", "3")
        reset_regional_emergency_settings()

        mock_tracker = MagicMock()
        # 2개 리전만 STRICT
        mock_tracker.get_all_active_namespaces.return_value = ["seoul", "tokyo"]
        mock_tracker.get_state.return_value = ScopedEmergencyState(
            namespace="test",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
        )

        detector = RegionalCascadeDetector(tracker=mock_tracker)
        result = detector.check_cascade_condition()

        # 임계값 3이므로 2개 리전으로는 cascade 미감지
        assert result["cascade_detected"] is False
        assert result["threshold"] == 3


class TestLegacyConstantsCompatibility:
    """하위 호환성 상수 테스트."""

    def test_tracker_legacy_constants_exist(self):
        """tracker.py의 레거시 상수 존재 확인."""
        from baldur.services.regional_emergency.tracker import (
            CACHE_TTL_SECONDS,
            DEFAULT_EMERGENCY_EXPIRY_HOURS,
        )

        # 기본값과 일치
        assert DEFAULT_EMERGENCY_EXPIRY_HOURS == 8
        assert CACHE_TTL_SECONDS == 30.0

    def test_cascade_detector_legacy_constants_exist(self):
        """cascade_detector.py의 레거시 상수 존재 확인."""
        from baldur.services.regional_emergency.cascade_detector import (
            DEFAULT_CASCADE_WINDOW_MINUTES,
            DEFAULT_ESCALATION_THRESHOLD,
        )

        # 기본값과 일치
        assert DEFAULT_ESCALATION_THRESHOLD == 2
        assert DEFAULT_CASCADE_WINDOW_MINUTES == 30
