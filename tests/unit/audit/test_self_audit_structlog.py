"""
Phase 3 수동 검수: self_audit.py structlog 전환 검증.

self_audit.py의 self._logger가 stdlib logging → structlog.BoundLogger로 전환되었는지,
로깅 동작이 올바르게 유지되는지 검증한다.

테스트 분류:
- Contract: structlog 전환 후 API 계약 검증 (이벤트 이름, 로깅 레벨 매핑)
- Behavior: SelfAuditLogger 동작이 기존과 동일하게 유지되는지 검증
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.audit.self_audit import SelfAuditEvent, SelfAuditLogger


@pytest.fixture(autouse=True)
def reset_singleton():
    """각 테스트 후 싱글톤 초기화."""
    yield
    SelfAuditLogger.reset_instance()


# ===========================================================================
# Contract Tests — `{component}.{action}` 이벤트 이름 계약
# ===========================================================================


class TestSelfAuditStructlogContract:
    """structlog 전환 후 self_audit 이벤트 이름 계약 검증."""

    def test_logger_is_structlog_bound_logger(self):
        """self._logger가 structlog.stdlib.BoundLogger 또는 BoundLoggerLazyProxy여야 한다."""
        instance = SelfAuditLogger()
        # structlog.get_logger()는 BoundLoggerLazyProxy를 반환
        logger_type_name = type(instance._logger).__name__
        assert "BoundLogger" in logger_type_name or "Proxy" in logger_type_name

    def test_event_name_is_self_audit_event(self):
        """structlog 로깅 시 event 이름이 'self_audit.event' 이어야 한다."""
        mock_logger = MagicMock()
        instance = SelfAuditLogger()
        instance._logger = mock_logger

        instance.log(SelfAuditEvent.STARTUP, "test message")

        # error/warning/info 중 하나가 호출돼야 함
        called = (
            mock_logger.info.called
            or mock_logger.warning.called
            or mock_logger.error.called
        )
        assert called

        # 첫 번째 인자가 'self_audit.event' 이어야 함
        all_calls = (
            mock_logger.info.call_args_list
            + mock_logger.warning.call_args_list
            + mock_logger.error.call_args_list
        )
        first_positional_args = [c.args[0] for c in all_calls if c.args]
        assert "self_audit.event" in first_positional_args


# ===========================================================================
# Behavior Tests — 로그 레벨 매핑 동작 검증
# ===========================================================================


class TestSelfAuditLogLevelBehavior:
    """SelfAuditLogger 로그 레벨 선택 동작 검증."""

    def test_failure_event_uses_error_method(self):
        """FAILURE_EVENTS에 속하는 이벤트는 logger.error()로 기록되어야 한다."""
        mock_logger = MagicMock()
        instance = SelfAuditLogger()
        instance._logger = mock_logger

        instance.log(SelfAuditEvent.WAL_WRITE_FAILED, "wal error")

        mock_logger.error.assert_called_once()
        mock_logger.warning.assert_not_called()
        mock_logger.info.assert_not_called()

    def test_circuit_opened_uses_warning_method(self):
        """CIRCUIT_OPENED 이벤트는 logger.warning()으로 기록되어야 한다."""
        mock_logger = MagicMock()
        instance = SelfAuditLogger()
        instance._logger = mock_logger

        instance.log(SelfAuditEvent.CIRCUIT_OPENED, "circuit opened")

        mock_logger.warning.assert_called_once()
        mock_logger.error.assert_not_called()

    def test_startup_event_uses_info_method(self):
        """STARTUP 이벤트는 logger.info()로 기록되어야 한다."""
        mock_logger = MagicMock()
        instance = SelfAuditLogger()
        instance._logger = mock_logger

        instance.log(SelfAuditEvent.STARTUP, "system started")

        mock_logger.info.assert_called_once()
        mock_logger.error.assert_not_called()
        mock_logger.warning.assert_not_called()

    def test_log_kwargs_contain_event_type_and_message(self):
        """structlog 호출 시 event_type, message가 키워드 인자로 전달되어야 한다."""
        mock_logger = MagicMock()
        instance = SelfAuditLogger()
        instance._logger = mock_logger

        instance.log(SelfAuditEvent.INITIALIZED, "ready", details={"key": "val"})

        all_calls = (
            mock_logger.info.call_args_list
            + mock_logger.warning.call_args_list
            + mock_logger.error.call_args_list
        )
        assert all_calls, "No log method was called"
        call = all_calls[0]
        assert call.kwargs.get("event_type") == SelfAuditEvent.INITIALIZED.value
        assert call.kwargs.get("message") == "ready"
        assert call.kwargs.get("details") == {"key": "val"}

    def test_details_omitted_when_none(self):
        """details=None인 경우 details 키가 없어야 한다."""
        mock_logger = MagicMock()
        instance = SelfAuditLogger()
        instance._logger = mock_logger

        instance.log(SelfAuditEvent.STARTUP, "no details")

        all_calls = (
            mock_logger.info.call_args_list
            + mock_logger.warning.call_args_list
            + mock_logger.error.call_args_list
        )
        assert all_calls
        call = all_calls[0]
        assert "details" not in call.kwargs


class TestSelfAuditStatsBehavior:
    """SelfAuditLogger 통계 누적 동작 검증."""

    def test_failure_event_increments_failure_count(self):
        """FAILURE_EVENTS 로깅 시 failure_events 카운터가 증가해야 한다."""
        instance = SelfAuditLogger()

        instance.log(SelfAuditEvent.RECOVERY_FAILED, "failed")

        stats = instance.get_stats()
        assert stats.failure_events == 1
        assert stats.total_events == 1

    def test_non_failure_event_does_not_increment_failure_count(self):
        """일반 이벤트는 failure_events를 증가시키지 않아야 한다."""
        instance = SelfAuditLogger()

        instance.log(SelfAuditEvent.STARTUP, "started")

        stats = instance.get_stats()
        assert stats.failure_events == 0
        assert stats.total_events == 1

    def test_log_always_succeeds_even_if_logger_raises(self):
        """내부 로거가 예외를 던져도 log()가 침묵해야 한다 (무한 루프 방지)."""
        mock_logger = MagicMock()
        mock_logger.error.side_effect = RuntimeError("logger broken")
        instance = SelfAuditLogger()
        instance._logger = mock_logger

        # 예외가 밖으로 전파되면 안 됨
        instance.log(SelfAuditEvent.RECOVERY_FAILED, "error")  # Should not raise
