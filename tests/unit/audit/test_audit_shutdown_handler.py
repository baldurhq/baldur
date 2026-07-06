"""
Tests for AuditShutdownHandler and graceful_shutdown once-guard (395 A4/C1).

Covers:
- AuditShutdownHandler ShutdownHandler contract (4 methods)
- graceful_shutdown_audit_system() idempotency guard
- _reset_audit_shutdown_state() test isolation
- Step order: disk-buffer teardown is the literal-final step (598 D4)
- _shutdown_disk_buffer step wrapper outcome branches (598 D4)
"""

import os
import threading
from unittest.mock import MagicMock, Mock, call, patch

import pytest
import structlog

# =============================================================================
# AuditShutdownHandler — Contract (§8.5 Dependency Interaction)
# =============================================================================


class TestAuditShutdownHandlerContract:
    """AuditShutdownHandler ShutdownHandler interface contract verification."""

    def test_implements_shutdown_handler_interface(self):
        """Implements every abstract method of the ShutdownHandler ABC."""
        from baldur.audit.shutdown_handler import AuditShutdownHandler
        from baldur.core.shutdown_coordinator import ShutdownHandler

        handler = AuditShutdownHandler()
        assert isinstance(handler, ShutdownHandler)

    def test_on_shutdown_start_does_not_raise(self):
        """on_shutdown_start() runs without raising."""
        from baldur.audit.shutdown_handler import AuditShutdownHandler

        handler = AuditShutdownHandler()
        handler.on_shutdown_start()  # Should not raise

    @patch(
        "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system",
        autospec=True,
    )
    def test_on_drain_complete_calls_graceful_shutdown(self, mock_shutdown):
        """on_drain_complete() calls graceful_shutdown_audit_system()."""
        from baldur.audit.shutdown_handler import AuditShutdownHandler

        handler = AuditShutdownHandler()
        handler.on_drain_complete()
        mock_shutdown.assert_called_once()

    @patch(
        "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system",
        autospec=True,
    )
    def test_on_force_shutdown_calls_graceful_shutdown(self, mock_shutdown):
        """on_force_shutdown() calls graceful_shutdown_audit_system()."""
        from baldur.audit.shutdown_handler import AuditShutdownHandler

        handler = AuditShutdownHandler()
        handler.on_force_shutdown(pending_requests=[])
        mock_shutdown.assert_called_once()

    @patch(
        "baldur.audit.async_audit_lifecycle.graceful_shutdown_audit_system",
        autospec=True,
    )
    def test_flush_failure_does_not_raise(self, mock_shutdown):
        """A graceful_shutdown failure does not propagate (fail-open)."""
        mock_shutdown.side_effect = Exception("flush error")
        from baldur.audit.shutdown_handler import AuditShutdownHandler

        handler = AuditShutdownHandler()
        # Should not raise
        handler.on_drain_complete()


# =============================================================================
# graceful_shutdown_audit_system — Idempotency Guard (§8.3)
# =============================================================================


class TestGracefulShutdownIdempotencyBehavior:
    """graceful_shutdown_audit_system() idempotency-guard behavior verification."""

    @pytest.fixture(autouse=True)
    def _reset_guard(self):
        """Reset once-guard for test isolation."""
        from baldur.audit.async_audit_lifecycle import (
            _reset_audit_shutdown_state,
        )

        _reset_audit_shutdown_state()
        yield
        _reset_audit_shutdown_state()

    @patch.dict(os.environ, {"BALDUR_TEST_MODE": "false"})
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_async_logger",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_sync_worker",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_wal",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._save_final_checkpoint",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_disk_buffer",
        autospec=True,
    )
    def test_double_call_executes_shutdown_only_once(
        self, mock_disk_buffer, mock_checkpoint, mock_wal, mock_sync, mock_logger
    ):
        """Calling twice executes the actual shutdown only once."""
        from baldur.audit.async_audit_lifecycle import (
            graceful_shutdown_audit_system,
        )

        graceful_shutdown_audit_system()
        graceful_shutdown_audit_system()

        mock_logger.assert_called_once()
        mock_sync.assert_called_once()
        mock_wal.assert_called_once()
        mock_checkpoint.assert_called_once()
        mock_disk_buffer.assert_called_once()

    @patch.dict(os.environ, {"BALDUR_TEST_MODE": "false"})
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_async_logger",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_sync_worker",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_wal",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._save_final_checkpoint",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_disk_buffer",
        autospec=True,
    )
    def test_reset_allows_re_execution(
        self, mock_disk_buffer, mock_checkpoint, mock_wal, mock_sync, mock_logger
    ):
        """Re-execution is possible after _reset_audit_shutdown_state()."""
        from baldur.audit.async_audit_lifecycle import (
            _reset_audit_shutdown_state,
            graceful_shutdown_audit_system,
        )

        graceful_shutdown_audit_system()
        _reset_audit_shutdown_state()
        graceful_shutdown_audit_system()

        assert mock_logger.call_count == 2

    @patch.dict(os.environ, {"BALDUR_TEST_MODE": "false"})
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_async_logger",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_sync_worker",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_wal",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._save_final_checkpoint",
        autospec=True,
    )
    @patch(
        "baldur.audit.async_audit_lifecycle._shutdown_disk_buffer",
        autospec=True,
    )
    def test_concurrent_calls_execute_once(
        self, mock_disk_buffer, mock_checkpoint, mock_wal, mock_sync, mock_logger
    ):
        """Concurrent multi-thread calls execute only once."""
        from baldur.audit.async_audit_lifecycle import (
            graceful_shutdown_audit_system,
        )

        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            graceful_shutdown_audit_system()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        mock_logger.assert_called_once()


# =============================================================================
# graceful_shutdown_audit_system — Step Order (§8.5)
# =============================================================================


class TestGracefulShutdownStepOrderBehavior:
    """Shutdown step ordering verification.

    The disk-buffer teardown must run literal-final: the PRO
    WAL-failure fallback writes INTO the disk buffer, so the buffer
    must outlive the WAL step, and checkpoint never touches it.
    """

    @pytest.fixture(autouse=True)
    def _reset_guard(self):
        """Reset once-guard for test isolation."""
        from baldur.audit.async_audit_lifecycle import (
            _reset_audit_shutdown_state,
        )

        _reset_audit_shutdown_state()
        yield
        _reset_audit_shutdown_state()

    @patch.dict(os.environ, {"BALDUR_TEST_MODE": "false"})
    def test_disk_buffer_teardown_runs_after_checkpoint_as_final_step(self):
        """Step order: logger -> sync -> WAL -> checkpoint -> disk buffer.

        Spec-less child mocks by design: they are attached to one
        parent Mock so mock_calls records the cross-step order.
        """
        from baldur.audit.async_audit_lifecycle import (
            graceful_shutdown_audit_system,
        )

        # Given — all 5 steps recorded on a single parent
        parent = Mock()
        with (
            patch(
                "baldur.audit.async_audit_lifecycle._shutdown_async_logger",
                new=parent.async_logger,
            ),
            patch(
                "baldur.audit.async_audit_lifecycle._shutdown_sync_worker",
                new=parent.sync_worker,
            ),
            patch(
                "baldur.audit.async_audit_lifecycle._shutdown_wal",
                new=parent.wal,
            ),
            patch(
                "baldur.audit.async_audit_lifecycle._save_final_checkpoint",
                new=parent.checkpoint,
            ),
            patch(
                "baldur.audit.async_audit_lifecycle._shutdown_disk_buffer",
                new=parent.disk_buffer,
            ),
        ):
            # When
            graceful_shutdown_audit_system()

        # Then — disk-buffer teardown is literal-final, after checkpoint
        assert parent.mock_calls == [
            call.async_logger(),
            call.sync_worker(),
            call.wal(),
            call.checkpoint(),
            call.disk_buffer(),
        ]


# =============================================================================
# _shutdown_disk_buffer step wrapper — Outcome Branches (§8.4)
# =============================================================================


class TestDiskBufferStepOutcomeBehavior:
    """Lifecycle step-5 wrapper outcome branches.

    The wrapper delegates to the signal-path module's teardown and
    converts its bool/raise outcome into the observable channel:
    INFO event on success, audit-metrics failure record + ERROR event
    on False return or exception.
    """

    def test_success_emits_closed_event_with_duration(self):
        """True return emits graceful_shutdown.disk_buffer_closed (INFO)."""
        from baldur.audit.async_audit_lifecycle import _shutdown_disk_buffer

        with (
            patch(
                "baldur.audit.persistence.disk_buffer_shutdown._shutdown_disk_buffer",
                autospec=True,
                return_value=True,
            ),
            structlog.testing.capture_logs() as cap_logs,
        ):
            _shutdown_disk_buffer()

        closed = [
            e for e in cap_logs if e["event"] == "graceful_shutdown.disk_buffer_closed"
        ]
        assert len(closed) == 1
        assert "duration_ms" in closed[0]

    def test_false_return_records_failure_and_emits_error(self):
        """False return records disk_buffer/shutdown_close failure + ERROR."""
        from baldur.audit.async_audit_lifecycle import _shutdown_disk_buffer

        mock_metrics = MagicMock()
        with (
            patch(
                "baldur.audit.persistence.disk_buffer_shutdown._shutdown_disk_buffer",
                autospec=True,
                return_value=False,
            ),
            patch(
                "baldur.audit.resilience.metrics.get_audit_metrics",
                return_value=mock_metrics,
            ),
            structlog.testing.capture_logs() as cap_logs,
        ):
            _shutdown_disk_buffer()

        mock_metrics.record_failure.assert_called_once_with(
            "disk_buffer", "shutdown_close"
        )
        errors = [
            e for e in cap_logs if e["event"] == "graceful_shutdown.disk_buffer_error"
        ]
        assert len(errors) == 1
        assert "duration_ms" in errors[0]

    def test_exception_records_failure_and_emits_error_with_cause(self):
        """A raised teardown records the failure and carries the error field."""
        from baldur.audit.async_audit_lifecycle import _shutdown_disk_buffer

        mock_metrics = MagicMock()
        boom = RuntimeError("teardown failed")
        with (
            patch(
                "baldur.audit.persistence.disk_buffer_shutdown._shutdown_disk_buffer",
                autospec=True,
                side_effect=boom,
            ),
            patch(
                "baldur.audit.resilience.metrics.get_audit_metrics",
                return_value=mock_metrics,
            ),
            structlog.testing.capture_logs() as cap_logs,
        ):
            _shutdown_disk_buffer()  # must not raise

        mock_metrics.record_failure.assert_called_once_with(
            "disk_buffer", "shutdown_close"
        )
        errors = [
            e for e in cap_logs if e["event"] == "graceful_shutdown.disk_buffer_error"
        ]
        assert len(errors) == 1
        assert errors[0]["error"] is boom
