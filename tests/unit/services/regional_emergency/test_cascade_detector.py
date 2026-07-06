"""
RegionalCascadeDetector 단위 테스트.

테스트 범위:
- Cascade 조건 감지 (check_cascade_condition)
- 자동/수동 GLOBAL 격상
- Cascade 이벤트 히스토리
- 싱글톤 패턴

Code reference:
    regional_emergency/cascade_detector.py
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.models.emergency import EmergencyLevel, EmergencyScope, ScopedEmergencyState
from baldur.services.regional_emergency.cascade_detector import (
    CascadeDetectionEvent,
    RegionalCascadeDetector,
    get_cascade_detector,
    reset_cascade_detector,
)


class TestCascadeDetectionEvent:
    """CascadeDetectionEvent 데이터클래스 테스트."""

    def test_default_values(self):
        """기본값 생성."""
        event = CascadeDetectionEvent()

        assert event.event_id == ""
        assert event.affected_regions == []
        assert event.total_strict_count == 0
        assert event.auto_escalated is False

    def test_to_dict(self):
        """딕셔너리 변환."""
        event = CascadeDetectionEvent(
            event_id="cascade-123",
            affected_regions=["seoul", "tokyo"],
            total_strict_count=2,
            threshold=2,
            auto_escalated=True,
            escalated_by="CascadeDetector",
        )

        result = event.to_dict()

        assert result["event_id"] == "cascade-123"
        assert result["affected_regions"] == ["seoul", "tokyo"]
        assert result["total_strict_count"] == 2
        assert result["auto_escalated"] is True


class TestCascadeDetection:
    """Cascade 감지 테스트."""

    @pytest.fixture
    def mock_tracker(self):
        """Mock NamespacedEmergencyTracker."""
        tracker = MagicMock()
        tracker.get_all_active_namespaces.return_value = []
        return tracker

    @pytest.fixture
    def detector(self, mock_tracker):
        """RegionalCascadeDetector with mock tracker."""
        return RegionalCascadeDetector(
            tracker=mock_tracker,
            escalation_threshold=2,
            auto_escalate=False,
        )

    def test_no_cascade_when_no_active_regions(self, detector, mock_tracker):
        """활성 리전 없을 때 cascade 미감지."""
        mock_tracker.get_all_active_namespaces.return_value = []

        result = detector.check_cascade_condition()

        assert result["cascade_detected"] is False
        assert result["strict_count"] == 0
        assert result["affected_regions"] == []

    def test_no_cascade_when_below_threshold(self, detector, mock_tracker):
        """임계값 미만일 때 cascade 미감지."""
        mock_tracker.get_all_active_namespaces.return_value = ["seoul"]
        mock_tracker.get_state.return_value = ScopedEmergencyState(
            namespace="seoul",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
        )

        result = detector.check_cascade_condition()

        assert result["cascade_detected"] is False
        assert result["strict_count"] == 1

    def test_cascade_detected_at_threshold(self, detector, mock_tracker):
        """임계값 도달 시 cascade 감지."""
        mock_tracker.get_all_active_namespaces.return_value = ["seoul", "tokyo"]
        mock_tracker.get_state.return_value = ScopedEmergencyState(
            namespace="test",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
        )

        result = detector.check_cascade_condition()

        assert result["cascade_detected"] is True
        assert result["strict_count"] == 2
        assert "seoul" in result["affected_regions"]
        assert "tokyo" in result["affected_regions"]

    def test_cascade_excludes_global(self, detector, mock_tracker):
        """Cascade 계산 시 global 네임스페이스 제외."""
        mock_tracker.get_all_active_namespaces.return_value = ["global", "seoul"]
        mock_tracker.get_state.return_value = ScopedEmergencyState(
            namespace="seoul",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
        )

        result = detector.check_cascade_condition()

        # global 제외하면 1개만 STRICT
        assert result["cascade_detected"] is False
        assert result["strict_count"] == 1
        assert "global" not in result["affected_regions"]

    def test_cascade_excludes_normal_regions(self, detector, mock_tracker):
        """NORMAL 상태 리전은 cascade 계산에서 제외."""
        mock_tracker.get_all_active_namespaces.return_value = [
            "seoul",
            "tokyo",
            "oregon",
        ]

        def get_state_side_effect(namespace):
            if namespace == "oregon":
                return ScopedEmergencyState(
                    namespace="oregon",
                    emergency_level=EmergencyLevel.NORMAL,
                    governance_mode="NORMAL",
                )
            return ScopedEmergencyState(
                namespace=namespace,
                emergency_level=EmergencyLevel.LEVEL_3,
                governance_mode="STRICT",
            )

        mock_tracker.get_state.side_effect = get_state_side_effect

        result = detector.check_cascade_condition()

        assert result["cascade_detected"] is True
        assert result["strict_count"] == 2
        assert "oregon" not in result["affected_regions"]


class TestAutoEscalation:
    """자동 GLOBAL 격상 테스트."""

    @pytest.fixture
    def mock_tracker(self):
        """Mock NamespacedEmergencyTracker."""
        tracker = MagicMock()
        tracker.get_all_active_namespaces.return_value = ["seoul", "tokyo"]
        tracker.get_state.return_value = ScopedEmergencyState(
            namespace="test",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
        )
        tracker.activate_emergency.return_value = ScopedEmergencyState(
            namespace="global",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
            scope=EmergencyScope.GLOBAL,
        )
        return tracker

    def test_auto_escalate_disabled_by_default(self, mock_tracker):
        """기본적으로 자동 격상 비활성화."""
        detector = RegionalCascadeDetector(
            tracker=mock_tracker,
            auto_escalate=False,
        )

        result = detector.check_cascade_condition()

        assert result["cascade_detected"] is True
        assert result["auto_escalated"] is False
        mock_tracker.activate_emergency.assert_not_called()

    def test_auto_escalate_when_enabled(self, mock_tracker):
        """auto_escalate=True일 때 자동 격상."""
        detector = RegionalCascadeDetector(
            tracker=mock_tracker,
            auto_escalate=True,
        )

        result = detector.check_cascade_condition()

        assert result["cascade_detected"] is True
        assert result["auto_escalated"] is True
        mock_tracker.activate_emergency.assert_called_once()

        # GLOBAL scope로 호출됐는지 확인
        call_kwargs = mock_tracker.activate_emergency.call_args.kwargs
        assert call_kwargs["scope"] == EmergencyScope.GLOBAL
        assert call_kwargs["level"] == EmergencyLevel.LEVEL_3


class TestManualEscalation:
    """수동 GLOBAL 격상 테스트."""

    @pytest.fixture
    def mock_tracker(self):
        """Mock NamespacedEmergencyTracker."""
        tracker = MagicMock()
        tracker.get_all_active_namespaces.return_value = ["seoul", "tokyo"]
        tracker.get_state.return_value = ScopedEmergencyState(
            namespace="test",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
        )
        tracker.activate_emergency.return_value = ScopedEmergencyState(
            namespace="global",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
            scope=EmergencyScope.GLOBAL,
        )
        return tracker

    @pytest.fixture
    def detector(self, mock_tracker):
        """RegionalCascadeDetector with mock tracker."""
        return RegionalCascadeDetector(tracker=mock_tracker)

    def test_manual_escalate_success(self, detector, mock_tracker):
        """수동 격상 성공."""
        result = detector.manual_escalate_to_global(
            escalated_by="admin@company.com",
            reason="Manual intervention required",
        )

        assert result["success"] is True
        assert result["escalated_to"] == "GLOBAL"
        assert result["escalated_by"] == "admin@company.com"
        mock_tracker.activate_emergency.assert_called_once()

    def test_manual_escalate_records_event(self, detector, mock_tracker):
        """수동 격상 시 이벤트 기록."""
        detector.manual_escalate_to_global(
            escalated_by="admin@company.com",
            reason="Test",
        )

        events = detector.get_recent_cascade_events(limit=1)

        assert len(events) == 1
        assert events[0]["escalated_by"] == "admin@company.com"
        assert events[0]["auto_escalated"] is False


class TestCascadeHistory:
    """Cascade 이벤트 히스토리 테스트."""

    @pytest.fixture
    def mock_tracker(self):
        """Mock NamespacedEmergencyTracker."""
        tracker = MagicMock()
        tracker.get_all_active_namespaces.return_value = ["seoul", "tokyo"]
        tracker.get_state.return_value = ScopedEmergencyState(
            namespace="test",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
        )
        return tracker

    @pytest.fixture
    def detector(self, mock_tracker):
        """RegionalCascadeDetector with mock tracker."""
        return RegionalCascadeDetector(tracker=mock_tracker)

    def test_cascade_detection_records_event(self, detector, mock_tracker):
        """Cascade 감지 시 이벤트 기록."""
        detector.check_cascade_condition()

        events = detector.get_recent_cascade_events()

        assert len(events) == 1
        assert "seoul" in events[0]["affected_regions"]

    def test_get_recent_events_respects_limit(self, detector, mock_tracker):
        """이벤트 조회 시 limit 적용."""
        # 여러 번 cascade 감지
        for _ in range(5):
            detector.check_cascade_condition()

        events = detector.get_recent_cascade_events(limit=3)

        assert len(events) == 3

    def test_get_recent_events_returns_newest_first(self, detector, mock_tracker):
        """최신 이벤트가 먼저 반환."""
        detector.check_cascade_condition()
        detector.check_cascade_condition()

        events = detector.get_recent_cascade_events()

        # 최신 이벤트가 먼저
        if len(events) >= 2:
            first_time = datetime.fromisoformat(events[0]["detected_at"])
            second_time = datetime.fromisoformat(events[1]["detected_at"])
            assert first_time >= second_time


class TestCustomThreshold:
    """사용자 정의 임계값 테스트."""

    @pytest.fixture
    def mock_tracker(self):
        """Mock NamespacedEmergencyTracker."""
        tracker = MagicMock()
        tracker.get_state.return_value = ScopedEmergencyState(
            namespace="test",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
        )
        return tracker

    def test_custom_threshold_3(self, mock_tracker):
        """임계값 3으로 설정."""
        mock_tracker.get_all_active_namespaces.return_value = ["seoul", "tokyo"]

        detector = RegionalCascadeDetector(
            tracker=mock_tracker,
            escalation_threshold=3,
        )

        result = detector.check_cascade_condition()

        # 2개만 STRICT이므로 cascade 미감지
        assert result["cascade_detected"] is False
        assert result["threshold"] == 3

    def test_custom_threshold_1(self, mock_tracker):
        """임계값 1로 설정 (단일 리전도 cascade)."""
        mock_tracker.get_all_active_namespaces.return_value = ["seoul"]

        detector = RegionalCascadeDetector(
            tracker=mock_tracker,
            escalation_threshold=1,
        )

        result = detector.check_cascade_condition()

        assert result["cascade_detected"] is True
        assert result["threshold"] == 1


class TestGetCascadeStatus:
    """get_cascade_status 테스트."""

    @pytest.fixture
    def mock_tracker(self):
        """Mock NamespacedEmergencyTracker."""
        tracker = MagicMock()
        tracker.get_all_active_namespaces.return_value = []
        return tracker

    @pytest.fixture
    def detector(self, mock_tracker):
        """RegionalCascadeDetector with mock tracker."""
        return RegionalCascadeDetector(tracker=mock_tracker)

    def test_get_cascade_status_same_as_check(self, detector, mock_tracker):
        """get_cascade_status는 check_cascade_condition과 동일."""
        status = detector.get_cascade_status()
        check = detector.check_cascade_condition()

        # 기본 필드들이 동일한지 확인
        assert status["cascade_detected"] == check["cascade_detected"]
        assert status["threshold"] == check["threshold"]


class TestSingleton:
    """싱글톤 패턴 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        reset_cascade_detector()

    def teardown_method(self):
        """테스트 후 싱글톤 초기화."""
        reset_cascade_detector()

    def test_singleton_returns_same_instance(self):
        """싱글톤이 같은 인스턴스 반환."""
        with patch(
            "baldur.services.regional_emergency.tracker.get_namespaced_emergency_tracker"
        ):
            detector1 = get_cascade_detector()
            detector2 = get_cascade_detector()

            assert detector1 is detector2

    def test_reset_clears_singleton(self):
        """reset이 싱글톤 초기화."""
        with patch(
            "baldur.services.regional_emergency.tracker.get_namespaced_emergency_tracker"
        ):
            detector1 = get_cascade_detector()
            reset_cascade_detector()
            detector2 = get_cascade_detector()

            assert detector1 is not detector2
