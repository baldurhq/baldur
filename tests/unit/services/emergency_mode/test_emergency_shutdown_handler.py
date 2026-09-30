"""
Tests for EmergencyModeShutdownHandler (395 C2).

Covers:
- ShutdownHandler interface contract
- on_shutdown_start calls the shutdown stop (not the operator's stop)
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
    """EmergencyModeShutdownHandler ShutdownHandler interface contract."""

    def test_implements_shutdown_handler_interface(self, handler):
        """Implements the ShutdownHandler ABC."""
        assert isinstance(handler, ShutdownHandler)

    def test_on_shutdown_start_calls_the_shutdown_stop(self, handler, mock_manager):
        """on_shutdown_start() calls the shutdown stop, never the operator's stop.

        The operator's stop clears any stored walk; the shutdown stop clears
        only a walk this process runs, so a worker that inherited a walk from
        its fork source leaves it alone.
        """
        handler.on_shutdown_start()
        mock_manager.stop_gradual_recovery_on_shutdown.assert_called_once()
        mock_manager.stop_gradual_recovery.assert_not_called()
        # Regression guard: the real method requires `stopped_by`; a bare
        # no-arg call raises TypeError in production (masked here only because
        # mock_manager is an unspec'd MagicMock).
        assert (
            "stopped_by"
            in mock_manager.stop_gradual_recovery_on_shutdown.call_args.kwargs
        )

    def test_on_force_shutdown_calls_the_shutdown_stop(self, handler, mock_manager):
        """on_force_shutdown() calls the shutdown stop as well."""
        handler.on_force_shutdown(pending_requests=[])
        mock_manager.stop_gradual_recovery_on_shutdown.assert_called_once()
        mock_manager.stop_gradual_recovery.assert_not_called()
        assert (
            "stopped_by"
            in mock_manager.stop_gradual_recovery_on_shutdown.call_args.kwargs
        )

    def test_on_drain_complete_is_noop(self, handler, mock_manager):
        """on_drain_complete() does nothing."""
        handler.on_drain_complete()
        # No exception raised is sufficient


# =============================================================================
# is_drain_complete — thread state detection (§8.8 State Transition)
# =============================================================================


class TestIsDrainCompleteBehavior:
    """is_drain_complete() recovery thread state detection behavior."""

    def test_drain_complete_when_no_thread(self, handler, mock_manager):
        """Returns True if the recovery thread is None."""
        mock_manager._recovery_thread = None
        assert handler.is_drain_complete() is True

    def test_drain_complete_when_thread_not_alive(self, handler, mock_manager):
        """Returns True if the recovery thread has terminated."""
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = False
        mock_manager._recovery_thread = mock_thread
        assert handler.is_drain_complete() is True

    def test_drain_not_complete_when_thread_alive(self, handler, mock_manager):
        """Returns False if the recovery thread is running."""
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True
        mock_thread.join = MagicMock()  # join(0.1) returns, thread still alive
        mock_manager._recovery_thread = mock_thread
        assert handler.is_drain_complete() is False
        mock_thread.join.assert_called_once_with(timeout=0.1)
