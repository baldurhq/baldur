"""
Tests for EmergencyModeShutdownHandler (395 C2).

Covers:
- ShutdownHandler interface contract
- on_shutdown_start calls stop_gradual_recovery
- is_drain_complete checks recovery thread state
- on_force_shutdown re-calls stop
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import threading
from unittest.mock import MagicMock

import pytest

from baldur.core.shutdown_coordinator import ShutdownHandler
from baldur_pro.services.emergency_mode.shutdown_handler import (
    EmergencyModeShutdownHandler,
)


@pytest.fixture
def mock_manager():
    """Create a mock GracefulDegradationManager.

    No spec: GracefulDegradationManager uses __new__ singleton that
    prevents MagicMock(spec=...) from working, and the handler accesses
    private _recovery_thread attribute which autospec would block.
    """
    manager = MagicMock()
    manager._recovery_thread = None
    manager.stop_gradual_recovery = MagicMock()
    return manager


@pytest.fixture
def handler(mock_manager):
    """Create EmergencyModeShutdownHandler with mock manager."""
    return EmergencyModeShutdownHandler(mock_manager)


# =============================================================================
# Contract (§8.5 Dependency Interaction)
# =============================================================================


class TestEmergencyModeShutdownHandlerContract:
    """EmergencyModeShutdownHandler ShutdownHandler 인터페이스 계약 검증."""

    def test_implements_shutdown_handler_interface(self, handler):
        """ShutdownHandler ABC를 구현한다."""
        assert isinstance(handler, ShutdownHandler)

    def test_on_shutdown_start_calls_stop_gradual_recovery(self, handler, mock_manager):
        """on_shutdown_start()가 stop_gradual_recovery(stopped_by=...)를 호출한다."""
        handler.on_shutdown_start()
        mock_manager.stop_gradual_recovery.assert_called_once()
        # Regression guard: the real GracefulDegradationManager
        # .stop_gradual_recovery requires `stopped_by`; a bare no-arg call
        # raises TypeError in production (masked here only because mock_manager
        # is an unspec'd MagicMock).
        assert "stopped_by" in mock_manager.stop_gradual_recovery.call_args.kwargs

    def test_on_force_shutdown_calls_stop_gradual_recovery(self, handler, mock_manager):
        """on_force_shutdown()이 stop_gradual_recovery(stopped_by=...)를 호출한다."""
        handler.on_force_shutdown(pending_requests=[])
        mock_manager.stop_gradual_recovery.assert_called_once()
        assert "stopped_by" in mock_manager.stop_gradual_recovery.call_args.kwargs

    def test_on_drain_complete_is_noop(self, handler, mock_manager):
        """on_drain_complete()는 아무 것도 하지 않는다."""
        handler.on_drain_complete()
        # No exception raised is sufficient


# =============================================================================
# is_drain_complete — thread state detection (§8.8 State Transition)
# =============================================================================


class TestIsDrainCompleteBehavior:
    """is_drain_complete() recovery thread 상태 감지 동작 검증."""

    def test_drain_complete_when_no_thread(self, handler, mock_manager):
        """recovery thread가 None이면 True를 반환한다."""
        mock_manager._recovery_thread = None
        assert handler.is_drain_complete() is True

    def test_drain_complete_when_thread_not_alive(self, handler, mock_manager):
        """recovery thread가 종료 상태이면 True를 반환한다."""
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = False
        mock_manager._recovery_thread = mock_thread
        assert handler.is_drain_complete() is True

    def test_drain_not_complete_when_thread_alive(self, handler, mock_manager):
        """recovery thread가 실행 중이면 False를 반환한다."""
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True
        mock_thread.join = MagicMock()  # join(0.1) returns, thread still alive
        mock_manager._recovery_thread = mock_thread
        assert handler.is_drain_complete() is False
        mock_thread.join.assert_called_once_with(timeout=0.1)
