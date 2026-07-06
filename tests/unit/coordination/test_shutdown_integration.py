"""
Leader Elector graceful-shutdown integration tests.

Since the OS signal handler removal, registration touches atexit only;
elector stop coverage at drain time lives in the coordinator-registered
``LeaderElectorShutdownHandler``.
"""
# Signal-handler removal coverage: 597 D6 (G5 clobber cascade).

from unittest.mock import MagicMock, patch

import pytest

from baldur.coordination.shutdown_integration import (
    _shutdown_state,
    integrate_with_shutdown_coordinator,
    register_for_graceful_shutdown,
    reset_registered_electors,
    shutdown_all_electors,
    unregister_from_graceful_shutdown,
)


@pytest.fixture(autouse=True)
def cleanup():
    """Reset the runtime-scoped registry (electors + atexit once-guard)
    before and after each test (450 Phase 4: state lives on the runtime)."""
    reset_registered_electors()
    yield
    reset_registered_electors()


class TestRegisterForGracefulShutdown:
    """register_for_graceful_shutdown tests."""

    def test_register_elector(self):
        """Registering an elector adds it to the runtime registry."""
        mock_elector = MagicMock()
        mock_elector.resource_name = "test-resource"

        register_for_graceful_shutdown(mock_elector)

        assert mock_elector in _shutdown_state().registered_electors

    def test_register_multiple_electors(self):
        """Multiple electors can be registered."""
        mock_elector1 = MagicMock()
        mock_elector1.resource_name = "resource-1"
        mock_elector2 = MagicMock()
        mock_elector2.resource_name = "resource-2"

        register_for_graceful_shutdown(mock_elector1)
        register_for_graceful_shutdown(mock_elector2)

        assert len(_shutdown_state().registered_electors) == 2

    def test_no_duplicate_registration(self):
        """Registering the same elector twice keeps a single entry."""
        mock_elector = MagicMock()
        mock_elector.resource_name = "test-resource"

        register_for_graceful_shutdown(mock_elector)
        register_for_graceful_shutdown(mock_elector)

        assert len(_shutdown_state().registered_electors) == 1


class TestUnregisterFromGracefulShutdown:
    """unregister_from_graceful_shutdown tests."""

    def test_unregister_elector(self):
        """Unregistering removes the elector from the registry."""
        mock_elector = MagicMock()
        mock_elector.resource_name = "test-resource"

        register_for_graceful_shutdown(mock_elector)
        assert mock_elector in _shutdown_state().registered_electors

        unregister_from_graceful_shutdown(mock_elector)
        assert mock_elector not in _shutdown_state().registered_electors

    def test_unregister_not_registered(self):
        """Unregistering a never-registered elector raises no error."""
        mock_elector = MagicMock()
        mock_elector.resource_name = "test-resource"

        # Not registered, but no error is raised
        unregister_from_graceful_shutdown(mock_elector)


class TestShutdownAllElectors:
    """shutdown_all_electors tests."""

    def test_stops_all_electors(self):
        """All registered electors are stopped and the registry is cleared."""
        mock_elector1 = MagicMock()
        mock_elector1.resource_name = "resource-1"
        mock_elector2 = MagicMock()
        mock_elector2.resource_name = "resource-2"

        _shutdown_state().registered_electors.append(mock_elector1)
        _shutdown_state().registered_electors.append(mock_elector2)

        shutdown_all_electors()

        mock_elector1.stop.assert_called_once()
        mock_elector2.stop.assert_called_once()
        assert len(_shutdown_state().registered_electors) == 0

    def test_handles_stop_exception(self):
        """A failing elector.stop() does not abort the sweep."""
        mock_elector = MagicMock()
        mock_elector.resource_name = "test-resource"
        mock_elector.stop.side_effect = Exception("Stop failed")

        _shutdown_state().registered_electors.append(mock_elector)

        # Continues despite the exception
        shutdown_all_electors()

        assert len(_shutdown_state().registered_electors) == 0


class TestIntegrateWithShutdownCoordinator:
    """integrate_with_shutdown_coordinator tests."""

    def test_returns_handler_when_available(self):
        """Returns a ShutdownHandler when the coordinator module exists."""
        # Only meaningful when the real shutdown_coordinator module is present
        result = integrate_with_shutdown_coordinator()
        # Must be None or a handler object
        assert result is None or hasattr(result, "on_shutdown_start")


class TestRegisterForGracefulShutdownAtexitOnceBehavior:
    """Registration installs atexit once and never an OS signal handler (597 D6).

    The former leader-elector SIGTERM/SIGINT handler replaced (did not
    chain) the installed disposition and, as the last registrant in the
    default init order, swallowed the signal for the whole process.
    """

    def test_repeated_registration_registers_atexit_once_and_no_signal_handler(self):
        """Two registrations → one atexit hook, zero signal.signal calls."""
        # Given
        mock_elector1 = MagicMock()
        mock_elector1.resource_name = "resource-1"
        mock_elector2 = MagicMock()
        mock_elector2.resource_name = "resource-2"

        # When
        with (
            patch("atexit.register") as mock_atexit,
            patch("signal.signal") as mock_signal,
        ):
            register_for_graceful_shutdown(mock_elector1)
            register_for_graceful_shutdown(mock_elector2)

        # Then — once-guard held; no OS signal handler installed
        mock_atexit.assert_called_once_with(shutdown_all_electors)
        mock_signal.assert_not_called()

    def test_reset_rearms_the_atexit_once_guard(self):
        """reset_registered_electors() re-arms the once-guard for a new runtime."""
        mock_elector = MagicMock()
        mock_elector.resource_name = "resource-1"

        with patch("atexit.register") as mock_atexit:
            register_for_graceful_shutdown(mock_elector)
            reset_registered_electors()
            register_for_graceful_shutdown(mock_elector)

        assert mock_atexit.call_count == 2


class TestLeaderElectorShutdownHandlerBehavior:
    """Coordinator-handler elector-stop coverage (597 D6 replacement path).

    Enabled deployments stop electors at drain start via this handler —
    the replacement for the deleted OS signal handler.
    """

    def test_on_shutdown_start_stops_registered_electors(self):
        """Drain start releases leadership for every registered elector."""
        # Given
        handler = integrate_with_shutdown_coordinator()
        assert handler is not None
        mock_elector = MagicMock()
        mock_elector.resource_name = "drain-start-elector"
        _shutdown_state().registered_electors.append(mock_elector)

        # When
        handler.on_shutdown_start()

        # Then
        mock_elector.stop.assert_called_once()
        assert len(_shutdown_state().registered_electors) == 0

    def test_on_force_shutdown_stops_registered_electors(self):
        """Forced shutdown also releases leadership."""
        # Given
        handler = integrate_with_shutdown_coordinator()
        assert handler is not None
        mock_elector = MagicMock()
        mock_elector.resource_name = "force-shutdown-elector"
        _shutdown_state().registered_electors.append(mock_elector)

        # When
        handler.on_force_shutdown(pending_requests=[])

        # Then
        mock_elector.stop.assert_called_once()
        assert len(_shutdown_state().registered_electors) == 0
