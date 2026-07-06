"""
IntegratedAuditRecorder Tests.

통합 감사 레코더 테스트.
Uses lazy imports to avoid Prometheus registry conflicts.
"""

from unittest.mock import MagicMock, Mock

import pytest


class TestIntegratedAuditRecorder:
    """IntegratedAuditRecorder 테스트."""

    def _create_mock_recorder(self):
        """Mock ResilientContinuousAuditRecorder 생성."""
        mock = MagicMock()
        mock._circuit_breaker = MagicMock()
        mock._circuit_breaker.state = MagicMock()
        mock._circuit_breaker.state.value = "closed"
        mock._record_with_integrity = MagicMock(return_value="audit-12345")
        mock.get_health_status = MagicMock(return_value={"healthy": True})
        mock.start = MagicMock()
        mock.stop = MagicMock()
        return mock

    def test_attach_observer(self):
        """Observer 등록."""
        from baldur.audit.audit_integration import (
            AuditEventObserver,
            IntegratedAuditRecorder,
        )

        mock_recorder = self._create_mock_recorder()
        integrated = IntegratedAuditRecorder(mock_recorder)

        observer = Mock(spec=AuditEventObserver)
        integrated.attach_observer(observer)

        assert len(integrated._observers) == 1

    def test_detach_observer(self):
        """Observer 해제."""
        from baldur.audit.audit_integration import (
            AuditEventObserver,
            IntegratedAuditRecorder,
        )

        mock_recorder = self._create_mock_recorder()
        integrated = IntegratedAuditRecorder(mock_recorder)

        observer = Mock(spec=AuditEventObserver)
        integrated.attach_observer(observer)
        integrated.detach_observer(observer)

        assert len(integrated._observers) == 0

    def test_attach_async_logger(self):
        """AsyncLoggerAdapter 연결."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            IntegratedAuditRecorder,
        )

        mock_recorder = self._create_mock_recorder()
        integrated = IntegratedAuditRecorder(mock_recorder)

        async_logger = AsyncLoggerAdapter()
        integrated.attach_async_logger(async_logger)

        assert integrated._async_logger == async_logger
        assert len(integrated._observers) == 1  # AsyncLoggerObserver 추가됨
        assert async_logger._running  # 자동 시작

        async_logger.stop()

    def test_notify_observers(self):
        """Observer 알림."""
        from baldur.audit.audit_integration import (
            AuditEventData,
            AuditEventObserver,
            AuditObserverEventType,
            IntegratedAuditRecorder,
        )

        mock_recorder = self._create_mock_recorder()
        integrated = IntegratedAuditRecorder(mock_recorder)

        observer1 = Mock(spec=AuditEventObserver)
        observer2 = Mock(spec=AuditEventObserver)
        integrated.attach_observer(observer1)
        integrated.attach_observer(observer2)

        event = AuditEventData(event_type=AuditObserverEventType.CIRCUIT_OPENED)
        integrated._notify_observers(event)

        observer1.on_event.assert_called_once_with(event)
        observer2.on_event.assert_called_once_with(event)

    def test_record_with_events_success(self):
        """record_with_events 성공 시 이벤트 전파."""
        from baldur.audit.audit_integration import (
            AuditEventObserver,
            AuditObserverEventType,
            IntegratedAuditRecorder,
        )

        mock_recorder = self._create_mock_recorder()
        integrated = IntegratedAuditRecorder(mock_recorder)

        observer = Mock(spec=AuditEventObserver)
        integrated.attach_observer(observer)

        entry = Mock()
        entry.action = "test_action"

        audit_id = integrated.record_with_events(entry)

        assert audit_id == "audit-12345"
        # RECORD_SUCCESS 이벤트가 전파되어야 함
        observer.on_event.assert_called()
        call_args = observer.on_event.call_args[0][0]
        assert call_args.event_type == AuditObserverEventType.RECORD_SUCCESS

    def test_record_with_events_failure(self):
        """record_with_events 실패 시 이벤트 전파."""
        from baldur.audit.audit_integration import (
            AuditEventObserver,
            AuditObserverEventType,
            IntegratedAuditRecorder,
        )

        mock_recorder = self._create_mock_recorder()
        mock_recorder._record_with_integrity.side_effect = Exception("DB error")

        integrated = IntegratedAuditRecorder(mock_recorder)

        observer = Mock(spec=AuditEventObserver)
        integrated.attach_observer(observer)

        entry = Mock()
        entry.action = "test_action"

        with pytest.raises(Exception, match="DB error"):
            integrated.record_with_events(entry)

        # RECORD_FAILED 이벤트가 전파되어야 함
        observer.on_event.assert_called()
        call_args = observer.on_event.call_args[0][0]
        assert call_args.event_type == AuditObserverEventType.RECORD_FAILED

    def test_circuit_state_change_detection(self):
        """Circuit Breaker 상태 변경 감지."""
        from baldur.audit.audit_integration import (
            AuditEventObserver,
            AuditObserverEventType,
            IntegratedAuditRecorder,
        )
        from baldur.audit.resilience import CircuitState

        mock_recorder = self._create_mock_recorder()
        # CircuitState enum을 직접 사용
        mock_recorder._circuit_breaker.state = CircuitState.CLOSED

        integrated = IntegratedAuditRecorder(mock_recorder)

        observer = Mock(spec=AuditEventObserver)
        integrated.attach_observer(observer)

        entry = Mock()
        entry.action = "test_action"

        # 첫 번째 record - CLOSED 상태
        integrated.record_with_events(entry)

        # Circuit OPEN으로 변경
        mock_recorder._circuit_breaker.state = CircuitState.OPEN

        # 두 번째 record - OPEN 감지
        integrated.record_with_events(entry)

        # CIRCUIT_OPENED 이벤트가 전파되어야 함
        calls = observer.on_event.call_args_list
        event_types = [call[0][0].event_type for call in calls]
        assert AuditObserverEventType.CIRCUIT_OPENED in event_types

    def test_get_health_status_with_async_logger(self):
        """AsyncLogger 포함 헬스 상태."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            IntegratedAuditRecorder,
        )

        mock_recorder = self._create_mock_recorder()
        integrated = IntegratedAuditRecorder(mock_recorder)

        async_logger = AsyncLoggerAdapter()
        integrated.attach_async_logger(async_logger)

        try:
            health = integrated.get_health_status()

            assert "async_logger" in health
            assert "observers_count" in health
            assert health["observers_count"] == 1
        finally:
            async_logger.stop()

    def test_start_stop(self):
        """시작/중지."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            IntegratedAuditRecorder,
        )

        mock_recorder = self._create_mock_recorder()
        integrated = IntegratedAuditRecorder(mock_recorder)

        async_logger = AsyncLoggerAdapter()
        integrated.attach_async_logger(async_logger)

        integrated.start()
        mock_recorder.start.assert_called()

        integrated.stop()
        mock_recorder.stop.assert_called()
        assert not async_logger._running
