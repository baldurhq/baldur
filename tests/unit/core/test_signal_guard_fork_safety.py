"""Unit tests for signal guard pattern across the signal-registering modules.

All modules skip signal.signal() registration when is_under_gunicorn()
returns True (SERVER_SOFTWARE-based). This prevents gunicorn's arbiter
or worker SIGTERM handlers from being overwritten by application code,
including the worker pre-post_worker_init window that the older
is_gunicorn_worker() guard (GUNICORN_WORKER env var) failed to cover.

Modules tested:
    - core/shutdown_coordinator.py — GracefulShutdownCoordinator.register_signals()
    - coordination/shutdown_integration.py — installs NO OS signal handler
      at all since 597 D6 (atexit + coordinator-handler coverage instead)
    - audit/persistence/disk_buffer.py — _register_signal_handlers()
    - adapters/audit/redis_buffer.py — RedisAuditBuffer._register_shutdown_hooks()

Note:
    audit/async_audit_lifecycle.py signal helpers were deleted in 416 Part 5;
    they were superseded by AuditShutdownHandler + GracefulShutdownCoordinator
    (see apps.py:323) and the Gunicorn worker_exit_cleanup hook (server.py:165).
    Coverage now lives in tests/unit/audit/test_audit_shutdown_handler.py and
    tests/unit/core/test_shutdown_coordinator.py.

Reference:
    docs/baldur/middleware_system/316_GUNICORN_PRELOAD_OPTIMIZATION.md §5.3
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestShutdownCoordinatorSignalGuardBehavior:
    """GracefulShutdownCoordinator.register_signals() skips under Gunicorn.

    The guard now uses ``is_under_gunicorn`` (SERVER_SOFTWARE-based) instead
    of ``is_gunicorn_worker`` (GUNICORN_WORKER-based) so the worker does not
    briefly overwrite gunicorn's SIGTERM handler before ``post_worker_init``
    sets the env var. See ``baldur.adapters.gunicorn.hooks`` and
    ``baldur.core.process_utils.is_under_gunicorn`` for the rationale.
    """

    @patch("baldur.core.process_utils.is_under_gunicorn", return_value=True)
    @patch("signal.signal")
    def test_skips_signal_registration_under_gunicorn(
        self, mock_signal, mock_under_gunicorn
    ):
        """Under Gunicorn (master OR worker), signal.signal() must NOT be
        called — gunicorn manages worker SIGTERM via its own arbiter and
        ``baldur.adapters.gunicorn.hooks.post_worker_init`` chains the
        worker drain on top of gunicorn's ``handle_exit``."""
        from baldur.core.shutdown_coordinator import GracefulShutdownCoordinator

        coordinator = GracefulShutdownCoordinator(request_tracker=MagicMock())
        coordinator.register_signals()

        mock_signal.assert_not_called()

    @patch("baldur.core.process_utils.is_under_gunicorn", return_value=False)
    @patch("signal.signal")
    def test_registers_signals_outside_gunicorn(self, mock_signal, mock_under_gunicorn):
        """Outside Gunicorn (dev server, plain Python, hypercorn, CLI),
        signal.signal() IS called for SIGTERM + SIGINT."""
        from baldur.core.shutdown_coordinator import GracefulShutdownCoordinator

        coordinator = GracefulShutdownCoordinator(request_tracker=MagicMock())
        coordinator.register_signals()

        assert mock_signal.call_count >= 1


class TestShutdownIntegrationNoSignalHandlerBehavior:
    """coordination/shutdown_integration installs no OS signal handler.

    The former leader-elector SIGTERM/SIGINT handler replaced (did not
    chain) the installed disposition and, being the last registrant in
    the default init order, swallowed the signal for the whole process.
    Deleted per 597 D6: coverage is the coordinator's bootstrap-wired
    shutdown handler (drain start), atexit (polite exits), and lease TTL
    expiry (ownerless death).
    """

    @patch("signal.signal")
    def test_register_for_graceful_shutdown_installs_no_signal_handler(
        self, mock_signal
    ):
        """Registration touches atexit only — never signal.signal()."""
        from baldur.coordination import shutdown_integration

        shutdown_integration.reset_registered_electors()
        elector = MagicMock()
        elector.resource_name = "fork-safety-test"
        try:
            with patch("atexit.register") as mock_atexit:
                shutdown_integration.register_for_graceful_shutdown(elector)

            mock_signal.assert_not_called()
            mock_atexit.assert_called_once_with(
                shutdown_integration.shutdown_all_electors
            )
        finally:
            shutdown_integration.reset_registered_electors()


class TestDiskBufferSignalGuardBehavior:
    """audit/persistence/disk_buffer._register_signal_handlers() guard."""

    @patch("baldur.core.process_utils.is_under_gunicorn", return_value=True)
    @patch("signal.getsignal")
    def test_skips_signal_registration_under_gunicorn(
        self, mock_getsignal, mock_under_gunicorn
    ):
        """Under gunicorn (master OR worker), returns early before getsignal."""
        from baldur.audit.persistence.disk_buffer import _register_signal_handlers

        _register_signal_handlers()

        # getsignal should NOT be called because we returned early
        mock_getsignal.assert_not_called()


class TestRedisBufferSignalGuardBehavior:
    """adapters/audit/redis_buffer.RedisAuditBuffer._register_shutdown_hooks() guard."""

    @patch("baldur.core.process_utils.is_under_gunicorn", return_value=True)
    @patch("signal.signal")
    def test_skips_signal_registration_under_gunicorn(
        self, mock_signal, mock_under_gunicorn
    ):
        """Under gunicorn (master OR worker), signal.signal() is NOT called but atexit IS.

        The guard MUST cover the worker pre-post_worker_init window that
        the older is_gunicorn_worker() check missed — even though the
        handler now chains (597 D7), installing it from a gunicorn worker
        would still add noise to gunicorn's own signal lifecycle.
        """
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        buffer = RedisAuditBuffer.__new__(RedisAuditBuffer)
        buffer._shutdown_registered = False
        buffer._graceful_shutdown = MagicMock()

        with patch("atexit.register") as mock_atexit:
            buffer._register_shutdown_hooks()

        mock_atexit.assert_called_once()
        mock_signal.assert_not_called()
        assert buffer._shutdown_registered is True
