"""
Emergency Postmortem Auto Trigger Tests (문서 146).

RecoveryCoordinator 복구 완료 시 자동 Postmortem 생성 테스트.

테스트 항목:
1. EMERGENCY_RECOVERY_COMPLETED 이벤트 발행 확인
2. Emergency Postmortem 핸들러 동작 확인
3. 설정 비활성화 시 스킵 확인
4. 최소 duration 미달 시 스킵 확인
5. Emergency Postmortem 데이터 구조 확인
6. approve_recovery() 시 이벤트 발행 확인
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest


class TestEmergencyRecoveryEventPublish:
    """RecoveryCoordinator 이벤트 발행 테스트."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # RecoveryCoordinator lives in baldur_pro (PRO-tier coordination).
        pytest.importorskip("baldur_pro")

    def test_complete_session_publishes_event(self):
        """_complete_session() 호출 시 EMERGENCY_RECOVERY_COMPLETED 이벤트 발행 확인."""
        from baldur.services.event_bus import EventType, get_event_bus
        from baldur_pro.services.coordination.enums import RecoveryStatus
        from baldur_pro.services.coordination.recovery_coordinator import (
            RecoveryCoordinator,
            reset_recovery_coordinator,
        )
        from baldur_pro.services.coordination.recovery_state import (
            RecoverySession,
            RecoveryStep,
            RecoveryStepType,
        )

        reset_recovery_coordinator()

        # EventBus 리셋
        bus = get_event_bus()
        bus.reset()

        # Mock backend
        mock_backend = MagicMock()
        mock_backend.get.return_value = None

        # Mock recovery lock
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.release.return_value = None

        coordinator = RecoveryCoordinator(
            backend=mock_backend,
            recovery_lock=mock_lock,
        )

        # 테스트용 세션 생성
        now = datetime.now(UTC).isoformat()
        session = RecoverySession(
            id="test-recovery-123",
            namespace="global",
            trigger_level="LEVEL_3",
            status=RecoveryStatus.IN_PROGRESS,
            steps=[
                RecoveryStep(
                    step_type=RecoveryStepType.BUDGET_RESET,
                    order=1,
                    wait_after_seconds=0,
                    params={"target_multiplier": 1.0},
                )
            ],
            current_step_index=1,
            started_at=now,
            initiated_by="system",
        )

        # 이벤트 수신 확인용
        received_events = []

        def capture_event(event):
            received_events.append(event)

        bus.subscribe(EventType.EMERGENCY_RECOVERY_COMPLETED, capture_event)

        # _complete_session 호출
        coordinator._complete_session(session)

        # 이벤트 발행 확인
        assert len(received_events) == 1
        event = received_events[0]
        assert event.event_type == EventType.EMERGENCY_RECOVERY_COMPLETED
        assert event.data["session_id"] == "test-recovery-123"
        assert event.data["namespace"] == "global"
        assert event.data["trigger_level"] == "LEVEL_3"
        assert event.data["steps_executed"] == 1
        assert event.source == "recovery_coordinator"

        # 정리
        bus.reset()
        reset_recovery_coordinator()

    def test_approve_recovery_publishes_event(self):
        """approve_recovery() 호출 시 EMERGENCY_RECOVERY_COMPLETED 이벤트 발행 확인."""
        from baldur.services.event_bus import EventType, get_event_bus
        from baldur_pro.services.coordination.recovery_coordinator import (
            RecoveryCoordinator,
            reset_recovery_coordinator,
        )

        reset_recovery_coordinator()

        bus = get_event_bus()
        bus.reset()

        # Mock backend - READY_TO_RESTORE 상태 세션 반환
        now = datetime.now(UTC).isoformat()
        session_data = {
            "id": "test-recovery-456",
            "namespace": "global",
            "trigger_level": "LEVEL_3",
            "status": "ready_to_restore",
            "steps": [
                {
                    "step_type": "budget_reset",
                    "order": 1,
                    "wait_after_seconds": 0,
                    "params": {"target_multiplier": 1.0},
                    "status": "completed",
                }
            ],
            "current_step_index": 1,
            "started_at": now,
            "initiated_by": "system",
            "metadata": {"requires_approval": True},
        }

        mock_backend = MagicMock()
        mock_backend.get.side_effect = lambda key: (
            "test-recovery-456"
            if "active" in key
            else session_data
            if "session" in key
            else None
        )

        mock_lock = MagicMock()
        mock_lock.release.return_value = None
        mock_lock.get_lock_owner.return_value = "test-recovery-456"

        coordinator = RecoveryCoordinator(
            backend=mock_backend,
            recovery_lock=mock_lock,
        )

        # 이벤트 수신 확인용
        received_events = []

        def capture_event(event):
            received_events.append(event)

        bus.subscribe(EventType.EMERGENCY_RECOVERY_COMPLETED, capture_event)

        # approve_recovery 호출
        coordinator.approve_recovery("global", "admin_user")

        # 이벤트 발행 확인
        assert len(received_events) == 1
        event = received_events[0]
        assert event.event_type == EventType.EMERGENCY_RECOVERY_COMPLETED
        assert event.data["session_id"] == "test-recovery-456"
        assert event.data["approved_by"] == "admin_user"
        assert event.data["requires_approval"] is True

        # 정리
        bus.reset()
        reset_recovery_coordinator()


class TestEmergencyPostmortemHandler:
    """Emergency Postmortem 핸들러 테스트."""

    def setup_method(self):
        """테스트 전 이벤트 버스 및 설정 리셋."""
        from baldur.services.event_bus import get_event_bus
        from baldur.settings.postmortem import reset_postmortem_settings

        self.bus = get_event_bus()
        self.bus.reset()
        reset_postmortem_settings()

    def teardown_method(self):
        """테스트 후 이벤트 버스 및 설정 리셋."""
        from baldur.settings.postmortem import reset_postmortem_settings

        self.bus.reset()
        reset_postmortem_settings()

    def test_handler_skips_when_disabled(self, monkeypatch):
        """auto_enabled=False일 때 핸들러 스킵 확인."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_emergency_recovery_completed_postmortem,
        )
        from baldur.settings.postmortem import reset_postmortem_settings

        reset_postmortem_settings()
        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_ENABLED", "false")

        event = BaldurEvent(
            event_type=EventType.EMERGENCY_RECOVERY_COMPLETED,
            data={
                "session_id": "test-session-123",
                "namespace": "global",
                "trigger_level": "LEVEL_3",
                "duration_seconds": 300,
            },
            source="recovery_coordinator",
        )

        with patch(
            "baldur.services.event_bus.bus._emergency_postmortem.logger"
        ) as mock_logger:
            _on_emergency_recovery_completed_postmortem(event)
            debug_calls = [str(call) for call in mock_logger.debug.call_args_list]
            assert any(
                "auto_postmortem_disabled_skipping" in call for call in debug_calls
            )

    def test_handler_skips_when_duration_below_min(self, monkeypatch):
        """duration이 최소 duration 미만일 때 스킵 확인."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_emergency_recovery_completed_postmortem,
        )
        from baldur.settings.postmortem import reset_postmortem_settings

        reset_postmortem_settings()
        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_ENABLED", "true")
        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_MIN_DURATION", "300")

        event = BaldurEvent(
            event_type=EventType.EMERGENCY_RECOVERY_COMPLETED,
            data={
                "session_id": "test-session-123",
                "namespace": "global",
                "trigger_level": "LEVEL_3",
                "duration_seconds": 60,  # 300초 미만
            },
            source="recovery_coordinator",
        )

        with patch(
            "baldur.services.event_bus.bus._emergency_postmortem.logger"
        ) as mock_logger:
            _on_emergency_recovery_completed_postmortem(event)
            debug_calls = [str(call) for call in mock_logger.debug.call_args_list]
            assert any("skipped" in call.lower() for call in debug_calls)

    def test_handler_generates_postmortem_when_enabled(self, monkeypatch):
        """auto_enabled=True일 때 Celery task으로 Postmortem 생성 위임 확인."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_emergency_recovery_completed_postmortem,
        )
        from baldur.settings.postmortem import reset_postmortem_settings

        reset_postmortem_settings()
        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_ENABLED", "true")
        monkeypatch.setenv("BALDUR_POSTMORTEM_AUTO_MIN_DURATION", "0")

        event = BaldurEvent(
            event_type=EventType.EMERGENCY_RECOVERY_COMPLETED,
            data={
                "session_id": "test-session-456",
                "namespace": "global",
                "trigger_level": "LEVEL_3",
                "started_at": datetime.now(UTC).isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_seconds": 300,
                "steps_executed": 4,
                "total_steps": 4,
                "requires_approval": False,
                "approved_by": None,
            },
            source="recovery_coordinator",
        )

        # Celery task .delay() mock
        with patch(
            "baldur.adapters.celery.tasks.postmortem.process_individual_postmortem.delay"
        ) as mock_delay:
            _on_emergency_recovery_completed_postmortem(event)

            # Celery task가 위임되었는지 확인
            mock_delay.assert_called_once()
            call_kwargs = mock_delay.call_args[1]
            assert call_kwargs["service_name"] == "emergency-global"
            assert call_kwargs["event_type"] == "emergency_recovery_completed"
            assert isinstance(call_kwargs["event_data"], dict)
            assert call_kwargs["event_data"]["data"]["session_id"] == "test-session-456"
            assert isinstance(call_kwargs["event_bus_history"], list)


class TestGenerateEmergencyPostmortemData:
    """Emergency Postmortem 데이터 생성 테스트."""

    def test_generate_emergency_postmortem_data_structure(self):
        """Emergency Postmortem 데이터 구조 확인."""
        from baldur.services.event_bus import _generate_emergency_postmortem_data

        session_data = {
            "session_id": "recovery-abc123",
            "namespace": "seoul",
            "trigger_level": "LEVEL_3",
            "started_at": "2026-01-28T10:00:00+00:00",
            "completed_at": "2026-01-28T10:10:00+00:00",
            "duration_seconds": 600,
            "steps_executed": 4,
            "total_steps": 4,
            "requires_approval": True,
            "approved_by": "admin",
        }

        event_bus_history = [
            {
                "event_type": "emergency_activated",
                "timestamp": "2026-01-28T10:00:00+00:00",
                "data": {"level": 3},
            },
            {
                "event_type": "emergency_recovery_completed",
                "timestamp": "2026-01-28T10:10:00+00:00",
                "data": {"session_id": "recovery-abc123"},
            },
        ]

        snapshot = {"cpu": 50, "memory": 60}

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=event_bus_history,
            snapshot=snapshot,
        )

        # 필수 필드 확인
        assert "incident_id" in result
        assert result["incident_id"].startswith("EMERGENCY-seoul")
        assert result["recovery_type"] == "emergency"
        assert result["namespace"] == "seoul"
        assert result["trigger_level"] == "LEVEL_3"
        assert result["recovery_session_id"] == "recovery-abc123"
        assert result["requires_approval"] is True
        assert result["approved_by"] == "admin"

        # 타임라인 확인
        assert "timeline" in result
        assert len(result["timeline"]) > 0

        # 복구 단계 확인
        assert "recovery_steps" in result
        assert len(result["recovery_steps"]) == 4

        # Action items 확인
        assert "auto_actions" in result
        assert "recommendations" in result

    def test_generate_emergency_postmortem_data_without_approval(self):
        """승인 없이 완료된 Emergency Postmortem 데이터 확인."""
        from baldur.services.event_bus import _generate_emergency_postmortem_data

        session_data = {
            "session_id": "recovery-def456",
            "namespace": "global",
            "trigger_level": "LEVEL_2",
            "started_at": "2026-01-28T11:00:00+00:00",
            "completed_at": "2026-01-28T11:05:00+00:00",
            "duration_seconds": 300,
            "steps_executed": 3,
            "total_steps": 3,
            "requires_approval": False,
            "approved_by": None,
        }

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=[],
            snapshot={},
        )

        assert result["requires_approval"] is False
        assert result["approved_by"] is None
        assert result["trigger_level"] == "LEVEL_2"

        # 승인 관련 action이 없어야 함
        action_descriptions = [a["action"] for a in result["auto_actions"]]
        assert "MANUAL_APPROVAL" not in action_descriptions

    def test_emergency_postmortem_includes_cb_events(self):
        """Emergency Postmortem 타임라인에 CB 이벤트 포함 확인."""
        from baldur.services.event_bus import _generate_emergency_postmortem_data

        session_data = {
            "session_id": "recovery-xyz",
            "namespace": "global",
            "trigger_level": "LEVEL_3",
            "started_at": "2026-01-28T12:00:00+00:00",
            "completed_at": "2026-01-28T12:15:00+00:00",
            "duration_seconds": 900,
            "steps_executed": 4,
            "total_steps": 4,
        }

        event_bus_history = [
            {
                "event_type": "emergency_activated",
                "timestamp": "2026-01-28T12:00:00+00:00",
                "data": {},
            },
            {
                "event_type": "circuit_breaker_opened",
                "timestamp": "2026-01-28T12:01:00+00:00",
                "data": {"service_name": "payment"},
            },
            {
                "event_type": "circuit_breaker_closed",
                "timestamp": "2026-01-28T12:10:00+00:00",
                "data": {"service_name": "payment"},
            },
            {
                "event_type": "emergency_recovery_completed",
                "timestamp": "2026-01-28T12:15:00+00:00",
                "data": {},
            },
        ]

        result = _generate_emergency_postmortem_data(
            session_data=session_data,
            event_bus_history=event_bus_history,
            snapshot={},
        )

        # 타임라인에 Emergency + CB 이벤트 모두 포함
        event_types = [e["event_type"] for e in result["timeline"]]
        assert "emergency_activated" in event_types
        assert "circuit_breaker_opened" in event_types
        assert "circuit_breaker_closed" in event_types
        assert "emergency_recovery_completed" in event_types


class TestEmergencyPostmortemHandlerRegistration:
    """핸들러 등록 확인 테스트."""

    def test_handler_registered_in_default_handlers(self):
        """register_default_handlers()에서 핸들러 등록 확인."""
        from baldur.services.event_bus import (
            EventType,
            get_event_bus,
            register_default_handlers,
        )

        bus = get_event_bus()
        bus.reset()

        register_default_handlers()

        subscriptions = bus.get_subscriptions(EventType.EMERGENCY_RECOVERY_COMPLETED)

        # EMERGENCY_RECOVERY_COMPLETED에 핸들러가 등록되어 있어야 함
        assert len(subscriptions) >= 1

        handler_names = [s["handler_name"] for s in subscriptions]
        assert "_on_emergency_recovery_completed_postmortem" in handler_names

        # Priority LOW로 등록되어 있어야 함
        for sub in subscriptions:
            if sub["handler_name"] == "_on_emergency_recovery_completed_postmortem":
                assert sub["priority"] == "LOW"

        bus.reset()
