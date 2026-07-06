"""
Integration Scenarios Tests.

통합 시나리오 테스트.
Uses lazy imports to avoid Prometheus registry conflicts.
"""

import time
from unittest.mock import MagicMock, Mock


class TestIntegrationScenarios:
    """통합 시나리오 테스트."""

    def test_full_flow_circuit_open_to_recovery(self):
        """전체 흐름: Circuit Open → 복구."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            IntegratedAuditRecorder,
        )
        from baldur.audit.resilience import CircuitState

        # 1. Setup
        flushed_events = []

        def callback(events):
            flushed_events.extend(events)

        mock_recorder = MagicMock()
        mock_recorder._circuit_breaker = MagicMock()
        # CircuitState enum 사용
        mock_recorder._circuit_breaker.state = CircuitState.CLOSED
        mock_recorder._record_with_integrity = MagicMock(return_value="audit-001")
        mock_recorder.get_health_status = MagicMock(return_value={"healthy": True})

        async_logger = AsyncLoggerAdapter(flush_callback=callback)
        integrated = IntegratedAuditRecorder(mock_recorder)
        integrated.attach_async_logger(async_logger)

        entry = Mock()
        entry.action = "test_action"

        try:
            # 2. 정상 동작
            integrated.record_with_events(entry)

            # 3. Circuit OPEN
            mock_recorder._circuit_breaker.state = CircuitState.OPEN
            integrated.record_with_events(entry)

            # 4. 복구 (HALF_OPEN → CLOSED)
            mock_recorder._circuit_breaker.state = CircuitState.HALF_OPEN
            integrated.record_with_events(entry)

            mock_recorder._circuit_breaker.state = CircuitState.CLOSED
            integrated.record_with_events(entry)

            time.sleep(0.2)

            # 5. 이벤트 검증
            event_types = [e.get("type") for e in flushed_events]
            states = [
                e.get("state")
                for e in flushed_events
                if e.get("type") == "circuit_breaker"
            ]

            # CB 이벤트가 있어야 함
            assert "circuit_breaker" in event_types
            assert "OPEN" in states

        finally:
            async_logger.stop()

    def test_multiple_observers(self):
        """다중 Observer 동시 동작."""
        from baldur.audit.audit_integration import (
            AuditEventObserver,
            IntegratedAuditRecorder,
        )

        mock_recorder = MagicMock()
        mock_recorder._circuit_breaker = MagicMock()
        mock_recorder._circuit_breaker.state = MagicMock()
        mock_recorder._circuit_breaker.state.value = "closed"
        mock_recorder._record_with_integrity = MagicMock(return_value="audit-001")

        integrated = IntegratedAuditRecorder(mock_recorder)

        # 여러 Observer 등록
        observer1 = Mock(spec=AuditEventObserver)
        observer2 = Mock(spec=AuditEventObserver)
        observer3 = Mock(spec=AuditEventObserver)

        integrated.attach_observer(observer1)
        integrated.attach_observer(observer2)
        integrated.attach_observer(observer3)

        entry = Mock()
        entry.action = "test"

        integrated.record_with_events(entry)

        # 모든 Observer가 호출되어야 함
        observer1.on_event.assert_called()
        observer2.on_event.assert_called()
        observer3.on_event.assert_called()

    def test_observer_error_isolation(self):
        """Observer 에러 격리."""
        from baldur.audit.audit_integration import (
            AuditEventObserver,
            IntegratedAuditRecorder,
        )

        mock_recorder = MagicMock()
        mock_recorder._circuit_breaker = MagicMock()
        mock_recorder._circuit_breaker.state = MagicMock()
        mock_recorder._circuit_breaker.state.value = "closed"
        mock_recorder._record_with_integrity = MagicMock(return_value="audit-001")

        integrated = IntegratedAuditRecorder(mock_recorder)

        # 에러 발생하는 Observer
        error_observer = Mock(spec=AuditEventObserver)
        error_observer.on_event.side_effect = Exception("Observer error")

        # 정상 Observer
        normal_observer = Mock(spec=AuditEventObserver)

        integrated.attach_observer(error_observer)
        integrated.attach_observer(normal_observer)

        entry = Mock()
        entry.action = "test"

        # 에러가 발생해도 다른 Observer는 호출됨
        integrated.record_with_events(entry)

        normal_observer.on_event.assert_called()
