"""
Convenience Functions Tests.

편의 함수 테스트.
Uses lazy imports to avoid Prometheus registry conflicts.
"""

from unittest.mock import MagicMock, Mock, patch


class TestConvenienceFunctions:
    """편의 함수 테스트."""

    def test_configure_integration(self):
        """configure_integration 함수."""
        from baldur.audit.audit_integration import (
            IntegratedAuditRecorder,
            configure_integration,
        )

        mock_recorder = MagicMock()
        mock_recorder._circuit_breaker = MagicMock()
        mock_recorder._circuit_breaker.state = MagicMock()
        mock_recorder._circuit_breaker.state.value = "closed"

        callback = Mock()

        integrated = configure_integration(
            resilient_recorder=mock_recorder,
            flush_callback=callback,
        )

        assert isinstance(integrated, IntegratedAuditRecorder)
        assert integrated._async_logger is not None
        assert len(integrated._observers) == 1

        integrated._async_logger.stop()

    def test_configure_integration_without_callback(self):
        """callback 없이 configure_integration."""
        from baldur.audit.audit_integration import configure_integration

        mock_recorder = MagicMock()
        mock_recorder._circuit_breaker = MagicMock()

        integrated = configure_integration(resilient_recorder=mock_recorder)

        assert integrated._async_logger is None
        assert len(integrated._observers) == 0

    def test_create_command_center_callback(self):
        """Command Center 콜백 생성."""
        from baldur.audit.audit_integration import create_command_center_callback

        callback = create_command_center_callback(
            endpoint="http://localhost:8000/api/events",
            timeout_seconds=3.0,
        )

        assert callable(callback)

    @patch("urllib.request.urlopen")
    def test_command_center_callback_success(self, mock_urlopen):
        """Command Center 콜백 성공."""
        from baldur.audit.audit_integration import create_command_center_callback

        mock_response = Mock()
        mock_response.status = 200
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_urlopen.return_value = mock_response

        callback = create_command_center_callback(
            endpoint="http://localhost:8000/api/events",
        )

        # 예외 없이 실행되어야 함
        callback([{"type": "test"}])

        mock_urlopen.assert_called_once()
