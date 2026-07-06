"""
Tests for Shutdown Coordinator - Graceful Shutdown

Framework-agnostic graceful shutdown implementation.
"""

import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, call, patch

import pytest

from baldur.core.shutdown_coordinator import (
    _SIGNAL_EXIT_DEADMAN_SECONDS,
    GracefulShutdownCoordinator,
    RequestState,
    RequestTracker,
    ShutdownHandler,
    ShutdownPhase,
    ShutdownStats,
    TrackedRequest,
    _classify_signal_disposition,
    _SignalDispositionMode,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def request_tracker():
    """Create a RequestTracker instance."""
    return RequestTracker()


@pytest.fixture
def shutdown_handler():
    """Create a mock shutdown handler."""
    handler = MagicMock(spec=ShutdownHandler)
    return handler


@pytest.fixture
def coordinator(request_tracker, shutdown_handler):
    """Create a GracefulShutdownCoordinator instance."""
    return GracefulShutdownCoordinator(
        request_tracker=request_tracker,
        drain_timeout=5.0,  # Short for testing
        shutdown_handler=shutdown_handler,
        check_interval=0.1,  # Fast for testing
    )


# =============================================================================
# ShutdownPhase Tests
# =============================================================================


class TestShutdownPhase:
    """Test ShutdownPhase enum."""

    def test_phases_exist(self):
        """All phases exist."""
        assert ShutdownPhase.RUNNING == "running"
        assert ShutdownPhase.DRAINING == "draining"
        assert ShutdownPhase.TERMINATING == "terminating"
        assert ShutdownPhase.TERMINATED == "terminated"


# =============================================================================
# RequestState Tests
# =============================================================================


class TestRequestState:
    """Test RequestState enum."""

    def test_states_exist(self):
        """All states exist."""
        assert RequestState.IN_PROGRESS == "in_progress"
        assert RequestState.COMPLETED == "completed"
        assert RequestState.ABORTED == "aborted"
        assert RequestState.TIMED_OUT == "timed_out"


# =============================================================================
# TrackedRequest Tests
# =============================================================================


class TestTrackedRequest:
    """Test TrackedRequest dataclass."""

    def test_request_creation(self):
        """TrackedRequest creation."""
        request = TrackedRequest(
            request_id="req-123",
            started_at=datetime.now(UTC),
            endpoint="/api/test",
            method="POST",
        )

        assert request.request_id == "req-123"
        assert request.endpoint == "/api/test"
        assert request.method == "POST"
        assert request.state == RequestState.IN_PROGRESS

    def test_duration_seconds(self):
        """duration_seconds calculation."""
        started = datetime.now(UTC) - timedelta(seconds=5)
        request = TrackedRequest(
            request_id="req-123",
            started_at=started,
        )

        assert request.duration_seconds >= 5.0
        assert request.duration_seconds < 6.0

    def test_metadata(self):
        """Metadata storage."""
        request = TrackedRequest(
            request_id="req-123",
            started_at=datetime.now(UTC),
            metadata={"user_id": 42, "trace_id": "abc"},
        )

        assert request.metadata["user_id"] == 42
        assert request.metadata["trace_id"] == "abc"


# =============================================================================
# ShutdownStats Tests
# =============================================================================


class TestShutdownStats:
    """Test ShutdownStats dataclass."""

    def test_stats_creation(self):
        """ShutdownStats creation."""
        stats = ShutdownStats(
            phase=ShutdownPhase.DRAINING,
            shutdown_started_at=datetime.now(UTC),
            in_flight_count=5,
            completed_during_drain=10,
            aborted_count=2,
            drain_timeout_seconds=30.0,
            remaining_drain_time=15.0,
        )

        assert stats.phase == ShutdownPhase.DRAINING
        assert stats.in_flight_count == 5
        assert stats.completed_during_drain == 10
        assert stats.remaining_drain_time == 15.0


# =============================================================================
# RequestTracker Tests
# =============================================================================


class TestRequestTracker:
    """Test RequestTracker."""

    def test_start_request(self, request_tracker):
        """Start tracking a request."""
        request = request_tracker.start_request(
            request_id="req-123",
            endpoint="/api/test",
            method="POST",
        )

        assert request.request_id == "req-123"
        assert request.state == RequestState.IN_PROGRESS

    def test_end_request_success(self, request_tracker):
        """End tracking a request (success)."""
        request_tracker.start_request("req-123")

        ended = request_tracker.end_request("req-123", success=True)

        assert ended is not None
        assert ended.state == RequestState.COMPLETED

    def test_end_request_failure(self, request_tracker):
        """End tracking a request (failure)."""
        request_tracker.start_request("req-123")

        ended = request_tracker.end_request("req-123", success=False)

        assert ended.state == RequestState.ABORTED

    def test_end_nonexistent_request(self, request_tracker):
        """Ending a nonexistent request returns None."""
        ended = request_tracker.end_request("nonexistent")

        assert ended is None

    def test_get_pending_requests(self, request_tracker):
        """Query pending requests."""
        request_tracker.start_request("req-1")
        request_tracker.start_request("req-2")
        request_tracker.end_request("req-1")

        pending = request_tracker.get_pending_requests()

        assert len(pending) == 1
        assert pending[0].request_id == "req-2"

    def test_get_pending_count(self, request_tracker):
        """Pending request count."""
        request_tracker.start_request("req-1")
        request_tracker.start_request("req-2")
        request_tracker.start_request("req-3")
        request_tracker.end_request("req-1")

        count = request_tracker.get_pending_count()

        assert count == 2

    def test_abort_all(self, request_tracker):
        """Abort all requests."""
        request_tracker.start_request("req-1")
        request_tracker.start_request("req-2")

        aborted, completed_count = request_tracker.abort_all()

        assert len(aborted) == 2
        for req in aborted:
            assert req.state == RequestState.ABORTED
        # Nothing completed before the abort — snapshot reads 0.
        assert completed_count == 0

    def test_abort_all_snapshot_includes_prior_completions(self, request_tracker):
        """abort_all returns the completed count from the same lock acquisition."""
        request_tracker.start_request("req-1")
        request_tracker.start_request("req-2")
        request_tracker.start_request("req-3")
        request_tracker.end_request("req-1")

        aborted, completed_count = request_tracker.abort_all()

        assert len(aborted) == 2
        assert completed_count == 1

    def test_completed_count(self, request_tracker):
        """Completed request count."""
        request_tracker.start_request("req-1")
        request_tracker.start_request("req-2")
        request_tracker.end_request("req-1")
        request_tracker.end_request("req-2")

        assert request_tracker.completed_count == 2


# =============================================================================
# GracefulShutdownCoordinator Tests
# =============================================================================


class TestGracefulShutdownCoordinator:
    """Test GracefulShutdownCoordinator."""

    def test_initial_phase(self, coordinator):
        """Initial phase."""
        assert coordinator.phase == ShutdownPhase.RUNNING

    def test_is_accepting_requests(self, coordinator):
        """Accepting-requests check."""
        assert coordinator.is_accepting_requests() is True

    def test_is_shutting_down(self, coordinator):
        """Shutting-down check."""
        assert coordinator.is_shutting_down() is False

    def test_initiate_shutdown(self, coordinator, shutdown_handler):
        """Initiate shutdown."""
        coordinator.initiate_shutdown()

        assert coordinator.phase in (ShutdownPhase.DRAINING, ShutdownPhase.TERMINATED)
        shutdown_handler.on_shutdown_start.assert_called_once()

    def test_initiate_shutdown_twice(self, coordinator):
        """Second initiate_shutdown attempt."""
        coordinator.initiate_shutdown()

        # The second call is ignored
        coordinator.initiate_shutdown()

        # Handled without error
        assert coordinator.phase != ShutdownPhase.RUNNING

    def test_initiate_shutdown_records_initiation_metric(self, coordinator):
        """initiate_shutdown increments baldur_shutdown_initiations_total counter.

        This metric is the canonical observability marker for "shutdown
        was initiated" — operators rely on it because structlog's first
        emit from an OS signal-handler context can be dropped, while the
        counter's shorter critical section survives.
        """
        from unittest.mock import patch

        with patch(
            "baldur.core.shutdown_coordinator.record_shutdown_initiated"
        ) as mock_record:
            coordinator.initiate_shutdown()

        mock_record.assert_called_once_with()

    def test_initiate_shutdown_metric_not_recorded_on_replay(self, coordinator):
        """Repeat initiate_shutdown calls do NOT re-record (idempotent guard)."""
        from unittest.mock import patch

        coordinator.initiate_shutdown()
        with patch(
            "baldur.core.shutdown_coordinator.record_shutdown_initiated"
        ) as mock_record:
            coordinator.initiate_shutdown()

        mock_record.assert_not_called()

    def test_not_accepting_after_shutdown(self, coordinator):
        """Requests rejected after shutdown."""
        coordinator.initiate_shutdown()

        assert coordinator.is_accepting_requests() is False

    def test_is_shutting_down_during_drain(self, coordinator, request_tracker):
        """is_shutting_down during drain."""
        # Add a request
        request_tracker.start_request("req-1")

        coordinator.initiate_shutdown()

        assert coordinator.is_shutting_down() is True

        # cleanup — finish the drain inside the test so the background
        # thread doesn't write shutdown metrics into a later test
        request_tracker.end_request("req-1")
        coordinator.wait_for_shutdown(timeout=2.0)

    def test_drain_completes_when_no_requests(self, coordinator, shutdown_handler):
        """Drain completes immediately when there are no requests."""
        coordinator.initiate_shutdown()

        # Wait briefly
        time.sleep(0.3)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        shutdown_handler.on_drain_complete.assert_called_once()

    def test_drain_waits_for_requests(
        self, coordinator, request_tracker, shutdown_handler
    ):
        """Drain waits for request completion."""
        # Start a request
        request_tracker.start_request("req-1")

        coordinator.initiate_shutdown()

        # Still draining
        time.sleep(0.1)
        assert coordinator.phase == ShutdownPhase.DRAINING

        # Complete the request
        request_tracker.end_request("req-1")

        # Wait for drain completion
        time.sleep(0.3)

        assert coordinator.phase == ShutdownPhase.TERMINATED

    def test_drain_timeout_force_shutdown(self, request_tracker, shutdown_handler):
        """Force shutdown on drain timeout."""
        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            drain_timeout=0.5,  # Very short timeout
            shutdown_handler=shutdown_handler,
            check_interval=0.1,
        )

        # Start a request (never completed)
        request_tracker.start_request("req-1")

        coordinator.initiate_shutdown()

        # Wait for the timeout
        time.sleep(0.8)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        shutdown_handler.on_force_shutdown.assert_called_once()

    def test_get_stats(self, coordinator, request_tracker):
        """Stats query."""
        request_tracker.start_request("req-1")

        stats = coordinator.get_stats()

        assert stats.phase == ShutdownPhase.RUNNING
        assert stats.in_flight_count == 1
        assert stats.shutdown_started_at is None

    def test_get_stats_during_drain(self, coordinator, request_tracker):
        """Stats query during drain."""
        request_tracker.start_request("req-1")

        coordinator.initiate_shutdown()

        stats = coordinator.get_stats()

        assert stats.phase == ShutdownPhase.DRAINING
        assert stats.shutdown_started_at is not None
        assert stats.remaining_drain_time is not None

        # cleanup — finish the drain inside the test so the background
        # thread doesn't write shutdown metrics into a later test
        request_tracker.end_request("req-1")
        coordinator.wait_for_shutdown(timeout=2.0)

    def test_wait_for_shutdown(self, coordinator):
        """Wait for shutdown."""
        coordinator.initiate_shutdown()

        result = coordinator.wait_for_shutdown(timeout=1.0)

        assert result is True
        assert coordinator.phase == ShutdownPhase.TERMINATED

    def test_wait_for_shutdown_not_initiated(self, coordinator):
        """Shutdown not initiated."""
        result = coordinator.wait_for_shutdown(timeout=0.1)

        assert result is False

    def test_clean_drain_reaches_terminated_and_notifies_handler(
        self, request_tracker, shutdown_handler
    ):
        """Clean drain reaches TERMINATED and fires the handler contract.

        Migrated off the removed on_shutdown_complete constructor seam:
        completion is observed via the phase and on_drain_complete.
        """
        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            shutdown_handler=shutdown_handler,
        )

        coordinator.initiate_shutdown()
        completed = coordinator.wait_for_shutdown(timeout=1.0)

        assert completed is True
        assert coordinator.phase == ShutdownPhase.TERMINATED
        shutdown_handler.on_drain_complete.assert_called_once()

    def test_clean_drain_records_drain_window_delta_not_lifetime_total(
        self, request_tracker, shutdown_handler
    ):
        """Clean drain records only requests completed during the drain window (596 D4).

        N requests complete before the drain and M during it —
        record_drained must be called with exactly M, not the
        process-lifetime N + M the tracker accumulates.
        """
        # Given — N=5 requests complete BEFORE the drain (process history)
        for i in range(5):
            request_tracker.start_request(f"pre-{i}")
            request_tracker.end_request(f"pre-{i}")
        # M=2 in-flight requests that will complete DURING the drain
        request_tracker.start_request("during-1")
        request_tracker.start_request("during-2")

        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            drain_timeout=5.0,
            shutdown_handler=shutdown_handler,
            check_interval=0.05,
        )

        # When — full clean drain cycle
        with (
            patch("baldur.core.shutdown_coordinator.record_drained") as mock_drained,
            patch("baldur.core.shutdown_coordinator.record_aborted") as mock_aborted,
        ):
            coordinator.initiate_shutdown()
            request_tracker.end_request("during-1")
            request_tracker.end_request("during-2")
            coordinator.wait_for_shutdown(timeout=3.0)

        # Then — drain-window delta only, no aborted on the clean path
        assert coordinator.phase == ShutdownPhase.TERMINATED
        mock_drained.assert_called_once_with(2)
        mock_aborted.assert_not_called()

    def test_forced_shutdown_records_aborted_and_pre_timeout_drained_delta(
        self, request_tracker, shutdown_handler
    ):
        """Forced path records the aborted count AND the pre-timeout drained delta (596 D5).

        A 0.5s drain that completes one request before timing out must
        report drained=1 alongside aborted=1 — not drained=0.
        """
        # Given — one pre-drain completion (excluded from the drain-window delta)
        request_tracker.start_request("pre-1")
        request_tracker.end_request("pre-1")
        # one request that completes during the drain + one that never finishes
        request_tracker.start_request("during-1")
        request_tracker.start_request("stuck-1")

        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            drain_timeout=0.5,
            shutdown_handler=shutdown_handler,
            check_interval=0.05,
        )

        # When — during-1 finishes inside the window, stuck-1 forces the timeout
        with (
            patch("baldur.core.shutdown_coordinator.record_aborted") as mock_aborted,
            patch("baldur.core.shutdown_coordinator.record_drained") as mock_drained,
        ):
            coordinator.initiate_shutdown()
            request_tracker.end_request("during-1")
            coordinator.wait_for_shutdown(timeout=3.0)

        # Then — both books recorded from the force-time snapshot
        assert coordinator.phase == ShutdownPhase.TERMINATED
        mock_aborted.assert_called_once_with(1)
        mock_drained.assert_called_once_with(1)

    def test_get_stats_completed_during_drain_zero_before_shutdown(
        self, coordinator, request_tracker
    ):
        """Before shutdown, completed_during_drain is the frozen 0, not tracker history (596 D6)."""
        request_tracker.start_request("req-1")
        request_tracker.end_request("req-1")

        stats = coordinator.get_stats()

        assert stats.phase == ShutdownPhase.RUNNING
        assert stats.completed_during_drain == 0

    def test_get_stats_returns_live_delta_while_draining(
        self, request_tracker, shutdown_handler
    ):
        """While DRAINING, completed_during_drain is the live drain-window delta (596 D6)."""
        # Given — pre-drain history of 1, two in-flight requests
        request_tracker.start_request("pre-1")
        request_tracker.end_request("pre-1")
        request_tracker.start_request("during-1")
        request_tracker.start_request("stuck-1")
        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            drain_timeout=5.0,
            shutdown_handler=shutdown_handler,
            check_interval=0.05,
        )
        coordinator.initiate_shutdown()

        # When — one request completes during the drain (stuck-1 keeps it DRAINING)
        request_tracker.end_request("during-1")

        # Then — live delta excludes the pre-drain history
        stats = coordinator.get_stats()
        assert stats.phase == ShutdownPhase.DRAINING
        assert stats.completed_during_drain == 1

        # cleanup — finish the drain inside the test so the background
        # thread doesn't write shutdown metrics into a later test
        request_tracker.end_request("stuck-1")
        coordinator.wait_for_shutdown(timeout=3.0)

    def test_get_stats_returns_frozen_delta_after_terminated(
        self, request_tracker, shutdown_handler
    ):
        """After TERMINATED, completed_during_drain freezes at the drain-window count (596 D6)."""
        # Given — 1 pre-drain completion, 2 requests that complete during the drain
        request_tracker.start_request("pre-1")
        request_tracker.end_request("pre-1")
        request_tracker.start_request("during-1")
        request_tracker.start_request("during-2")
        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            drain_timeout=5.0,
            shutdown_handler=shutdown_handler,
            check_interval=0.05,
        )

        # When — full clean drain cycle
        coordinator.initiate_shutdown()
        request_tracker.end_request("during-1")
        request_tracker.end_request("during-2")
        coordinator.wait_for_shutdown(timeout=3.0)

        # Then — frozen at the drain-window delta, not the lifetime 3
        stats = coordinator.get_stats()
        assert stats.phase == ShutdownPhase.TERMINATED
        assert stats.completed_during_drain == 2


# =============================================================================
# Signal Handler Tests
# =============================================================================


class TestSignalHandler:
    """Test signal handler registration."""

    @patch("signal.signal")
    def test_register_signals(self, mock_signal, coordinator, monkeypatch):
        """Signal handler registration."""
        import signal as signal_module

        # Ensure non-gunicorn context so the registration runs
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)
        monkeypatch.delenv("SERVER_SOFTWARE", raising=False)

        coordinator.register_signals()

        # Verify SIGTERM and SIGINT registration
        calls = mock_signal.call_args_list
        assert len(calls) == 2

        registered_signals = [call[0][0] for call in calls]
        assert signal_module.SIGTERM in registered_signals
        assert signal_module.SIGINT in registered_signals

    @patch("signal.signal")
    def test_register_signals_skipped_under_gunicorn_master(
        self, mock_signal, coordinator, monkeypatch
    ):
        """SERVER_SOFTWARE=gunicorn/x in master process — skip even
        before any worker has post_worker_init'd."""
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/25.3.0")

        coordinator.register_signals()

        mock_signal.assert_not_called()

    @patch("signal.signal")
    def test_register_signals_skipped_in_gunicorn_worker(
        self, mock_signal, coordinator, monkeypatch
    ):
        """In a gunicorn worker (post_worker_init has set
        GUNICORN_WORKER=1), signal registration must be skipped so
        gunicorn's worker_int hook can still fire."""
        monkeypatch.setenv("GUNICORN_WORKER", "1")
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/25.3.0")

        coordinator.register_signals()

        mock_signal.assert_not_called()

    @patch("signal.signal")
    def test_register_signals_skipped_in_gunicorn_worker_pre_post_init(
        self, mock_signal, coordinator, monkeypatch
    ):
        """Critical guard: during the worker's WSGI import phase,
        baldur.init() runs BEFORE post_worker_init has set
        GUNICORN_WORKER=1 — but SERVER_SOFTWARE is already inherited
        from master via fork. Without the SERVER_SOFTWARE check, the
        worker would briefly overwrite gunicorn's SIGTERM handler,
        suppressing worker_int and breaking graceful drain."""
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/25.3.0")

        coordinator.register_signals()

        mock_signal.assert_not_called()


# =============================================================================
# Thread Safety Tests
# =============================================================================


class TestThreadSafety:
    """Test thread safety."""

    def test_concurrent_request_tracking(self, request_tracker):
        """Concurrent request tracking."""
        errors = []

        def track_requests(prefix):
            try:
                for i in range(100):
                    req_id = f"{prefix}-{i}"
                    request_tracker.start_request(req_id)
                    request_tracker.end_request(req_id)
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=track_requests, args=(f"t{i}",)) for i in range(3)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_concurrent_status_reads(self, coordinator, request_tracker):
        """Concurrent stats reads."""
        results = []

        def read_stats():
            for _ in range(50):
                coordinator.get_stats()
            results.append(True)

        threads = [threading.Thread(target=read_stats) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases."""

    def test_handler_exception_on_start(self, request_tracker):
        """Handler on_shutdown_start exception."""
        handler = MagicMock(spec=ShutdownHandler)
        handler.on_shutdown_start.side_effect = Exception("Handler error")

        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            shutdown_handler=handler,
        )

        # Shutdown proceeds despite the exception
        coordinator.initiate_shutdown()

        assert coordinator.phase != ShutdownPhase.RUNNING

    def test_request_with_metadata(self, request_tracker):
        """Request with metadata."""
        request = request_tracker.start_request(
            request_id="req-123",
            endpoint="/api/test",
            method="POST",
            metadata={"user_id": 42, "trace_id": "abc-123"},
        )

        assert request.metadata["user_id"] == 42
        assert request.metadata["trace_id"] == "abc-123"

    def test_empty_request_tracker(self, coordinator):
        """Empty request tracker."""
        stats = coordinator.get_stats()

        assert stats.in_flight_count == 0

    def test_zero_drain_timeout(self, request_tracker, shutdown_handler):
        """Zero drain timeout."""
        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            drain_timeout=0.0,  # Immediate force shutdown
            shutdown_handler=shutdown_handler,
            check_interval=0.1,
        )

        request_tracker.start_request("req-1")

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=1.0)

        # Forced shutdown immediately
        assert coordinator.phase == ShutdownPhase.TERMINATED

    def test_very_long_running_request(self, request_tracker):
        """Very long-running request."""
        started = datetime.now(UTC) - timedelta(hours=1)

        request = TrackedRequest(
            request_id="long-req",
            started_at=started,
        )

        # Running for over an hour
        assert request.duration_seconds >= 3600


# =============================================================================
# ShutdownHandler Abstract Tests
# =============================================================================


class TestShutdownHandlerInterface:
    """Test ShutdownHandler interface."""

    def test_handler_methods(self, shutdown_handler):
        """Handler methods exist."""
        assert hasattr(shutdown_handler, "on_shutdown_start")
        assert hasattr(shutdown_handler, "on_drain_complete")
        assert hasattr(shutdown_handler, "on_force_shutdown")

    def test_custom_handler_implementation(self):
        """Custom handler implementation."""

        class CustomHandler(ShutdownHandler):
            def __init__(self):
                self.started = False
                self.drained = False
                self.forced = False

            def on_shutdown_start(self):
                self.started = True

            def on_drain_complete(self):
                self.drained = True

            def on_force_shutdown(self, pending_requests):
                self.forced = True

        handler = CustomHandler()
        tracker = RequestTracker()

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            shutdown_handler=handler,
        )

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=1.0)

        assert handler.started is True
        assert handler.drained is True


# =============================================================================
# 386: ShutdownHandler ABC Extension Tests
# =============================================================================


class TestShutdownHandlerIsDrainCompleteBehavior:
    """is_drain_complete() default behavior (386 §0a)."""

    def test_is_drain_complete_default_returns_true(self):
        """Default is_drain_complete() returns True for backward compat."""

        class MinimalHandler(ShutdownHandler):
            def on_shutdown_start(self):
                pass

            def on_drain_complete(self):
                pass

            def on_force_shutdown(self, pending_requests):
                pass

        handler = MinimalHandler()
        assert handler.is_drain_complete() is True

    def test_is_drain_complete_can_be_overridden(self):
        """Subclass can override is_drain_complete() to control drain."""

        class SlowHandler(ShutdownHandler):
            def __init__(self):
                self.drained = False

            def on_shutdown_start(self):
                pass

            def is_drain_complete(self) -> bool:
                return self.drained

            def on_drain_complete(self):
                pass

            def on_force_shutdown(self, pending_requests):
                pass

        handler = SlowHandler()
        assert handler.is_drain_complete() is False

        handler.drained = True
        assert handler.is_drain_complete() is True


# =============================================================================
# 386: Multi-Handler Support Tests
# =============================================================================


class TestRegisterHandlerBehavior:
    """register_handler() behavior (386 §0b, D-11)."""

    def test_register_handler_during_running_phase_succeeds(self):
        """Handler can be registered during RUNNING phase."""
        tracker = RequestTracker()
        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            check_interval=0.1,
        )
        handler = MagicMock(spec=ShutdownHandler)

        coordinator.register_handler(handler)
        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=1.0)

        handler.on_shutdown_start.assert_called_once()

    def test_register_handler_after_shutdown_raises_shutdown_error(self):
        """Handler registration after shutdown started raises ShutdownError."""
        from baldur.core.shutdown_coordinator import ShutdownError

        tracker = RequestTracker()
        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            check_interval=0.1,
        )

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=1.0)

        handler = MagicMock(spec=ShutdownHandler)
        with pytest.raises(ShutdownError, match="Cannot register shutdown handlers"):
            coordinator.register_handler(handler)


class TestMultiHandlerBehavior:
    """Multi-handler support in GracefulShutdownCoordinator (386 §0b)."""

    def test_multiple_handlers_all_notified_on_shutdown(self):
        """All registered handlers receive on_shutdown_start."""
        tracker = RequestTracker()
        handler1 = MagicMock(spec=ShutdownHandler)
        handler2 = MagicMock(spec=ShutdownHandler)

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            shutdown_handler=handler1,
            check_interval=0.1,
        )
        coordinator.register_handler(handler2)

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=1.0)

        handler1.on_shutdown_start.assert_called_once()
        handler2.on_shutdown_start.assert_called_once()

    def test_multiple_handlers_all_receive_on_drain_complete(self):
        """All handlers receive on_drain_complete on successful drain."""
        tracker = RequestTracker()
        handler1 = MagicMock(spec=ShutdownHandler)
        handler2 = MagicMock(spec=ShutdownHandler)

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            shutdown_handler=handler1,
            check_interval=0.1,
        )
        coordinator.register_handler(handler2)

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=1.0)

        handler1.on_drain_complete.assert_called_once()
        handler2.on_drain_complete.assert_called_once()

    def test_multiple_handlers_all_receive_on_force_shutdown(self):
        """All handlers receive on_force_shutdown on timeout."""
        tracker = RequestTracker()
        tracker.start_request("req-stuck")

        handler1 = MagicMock(spec=ShutdownHandler)
        handler2 = MagicMock(spec=ShutdownHandler)

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=0.2,
            shutdown_handler=handler1,
            check_interval=0.05,
        )
        coordinator.register_handler(handler2)

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=2.0)

        handler1.on_force_shutdown.assert_called_once()
        handler2.on_force_shutdown.assert_called_once()

    def test_handler_start_exception_does_not_block_other_handlers(self):
        """Exception in one handler's on_shutdown_start doesn't block others."""
        tracker = RequestTracker()
        handler1 = MagicMock(spec=ShutdownHandler)
        handler1.on_shutdown_start.side_effect = RuntimeError("boom")

        handler2 = MagicMock(spec=ShutdownHandler)

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            shutdown_handler=handler1,
            check_interval=0.1,
        )
        coordinator.register_handler(handler2)

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=1.0)

        # handler2 still gets called despite handler1 failure
        handler2.on_shutdown_start.assert_called_once()


# =============================================================================
# 386: Unified Drain Loop Tests
# =============================================================================


class TestUnifiedDrainLoopBehavior:
    """Unified drain loop: HTTP drain + handler drain concurrent (386 §0c)."""

    def test_drain_waits_for_handler_is_drain_complete(self):
        """Drain loop waits until handler.is_drain_complete() returns True."""

        class DelayedDrainHandler(ShutdownHandler):
            def __init__(self):
                self.call_count = 0

            def on_shutdown_start(self):
                pass

            def is_drain_complete(self) -> bool:
                self.call_count += 1
                # Return False for first 2 polls, then True
                return self.call_count > 2

            def on_drain_complete(self):
                pass

            def on_force_shutdown(self, pending_requests):
                pass

        handler = DelayedDrainHandler()
        tracker = RequestTracker()

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=5.0,
            shutdown_handler=handler,
            check_interval=0.05,
        )

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=2.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        assert handler.call_count > 2

    def test_drain_loop_timeout_calls_force_shutdown_on_slow_handler(self):
        """If handler never drains, force shutdown is triggered."""

        class NeverDrainHandler(ShutdownHandler):
            def __init__(self):
                self.force_called = False

            def on_shutdown_start(self):
                pass

            def is_drain_complete(self) -> bool:
                return False

            def on_drain_complete(self):
                pass

            def on_force_shutdown(self, pending_requests):
                self.force_called = True

        handler = NeverDrainHandler()
        tracker = RequestTracker()

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=0.2,
            shutdown_handler=handler,
            check_interval=0.05,
        )

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=2.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        assert handler.force_called is True

    def test_on_drain_complete_exception_does_not_block_other_handlers(self):
        """Exception in one handler's on_drain_complete doesn't block the rest.

        Migrated off the removed on_shutdown_complete seam: the surviving
        completion contract is the remaining handlers + TERMINATED phase.
        """
        tracker = RequestTracker()
        handler1 = MagicMock(spec=ShutdownHandler)
        handler1.on_drain_complete.side_effect = RuntimeError("drain error")
        handler2 = MagicMock(spec=ShutdownHandler)

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            shutdown_handler=handler1,
            check_interval=0.1,
        )
        coordinator.register_handler(handler2)

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=1.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        handler2.on_drain_complete.assert_called_once()

    def test_on_force_shutdown_exception_does_not_block_phase_terminated(self):
        """Exception in on_force_shutdown doesn't prevent TERMINATED phase."""
        tracker = RequestTracker()
        tracker.start_request("req-stuck")

        handler1 = MagicMock(spec=ShutdownHandler)
        handler1.on_force_shutdown.side_effect = RuntimeError("force error")
        handler2 = MagicMock(spec=ShutdownHandler)

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=0.2,
            shutdown_handler=handler1,
            check_interval=0.05,
        )
        coordinator.register_handler(handler2)

        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=2.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        handler2.on_force_shutdown.assert_called_once()


class TestSafeIsDrainCompleteBehavior:
    """_safe_is_drain_complete exception safety (386 §0c)."""

    def test_returns_true_on_handler_exception(self):
        """Exception in is_drain_complete returns True to avoid blocking."""
        handler = MagicMock(spec=ShutdownHandler)
        handler.is_drain_complete.side_effect = RuntimeError("check error")

        tracker = RequestTracker()
        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            shutdown_handler=handler,
            check_interval=0.1,
        )

        # Drain should complete since exception → True
        coordinator.initiate_shutdown()
        coordinator.wait_for_shutdown(timeout=1.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        handler.on_drain_complete.assert_called_once()


# =============================================================================
# 597: Signal Lifecycle Tests (disposition classification + exit trampoline)
# =============================================================================


def _marked_chain_handler(tail):
    """Build a Baldur-style chained closure carrying the chain-walk marker.

    Mirrors the shape produced by disk_buffer/redis_buffer chained
    handlers: a callable head whose ``_baldur_chained_original`` points
    at the captured prior disposition.
    """

    def _chained(signum, frame):
        pass

    _chained._baldur_chained_original = tail
    return _chained


class TestSignalDispositionClassificationBehavior:
    """_classify_signal_disposition chain-walk verdicts (597 D2).

    The verdict must follow the effective tail through Baldur chain
    markers so registration order cannot flip it.
    """

    @pytest.mark.parametrize(
        ("build_original", "expected"),
        [
            (lambda: signal.SIG_DFL, _SignalDispositionMode.DEFER_EXIT),
            (lambda: signal.SIG_IGN, _SignalDispositionMode.SKIP_IGNORED),
            (lambda: None, _SignalDispositionMode.SKIP_UNKNOWN),
            (lambda: signal.default_int_handler, _SignalDispositionMode.CHAIN),
            (
                lambda: lambda signum, frame: None,
                _SignalDispositionMode.CHAIN,
            ),
            (
                lambda: _marked_chain_handler(signal.SIG_DFL),
                _SignalDispositionMode.DEFER_EXIT,
            ),
            (
                lambda: _marked_chain_handler(lambda signum, frame: None),
                _SignalDispositionMode.CHAIN,
            ),
            (
                lambda: _marked_chain_handler(signal.SIG_IGN),
                _SignalDispositionMode.SKIP_IGNORED,
            ),
            (
                lambda: _marked_chain_handler(None),
                _SignalDispositionMode.SKIP_UNKNOWN,
            ),
            (
                lambda: _marked_chain_handler(_marked_chain_handler(signal.SIG_DFL)),
                _SignalDispositionMode.DEFER_EXIT,
            ),
        ],
        ids=[
            "sig_dfl_defer_exit",
            "sig_ign_skip",
            "c_level_none_skip",
            "default_int_handler_chain",
            "foreign_callable_chain",
            "marked_chain_sig_dfl_tail_defer_exit",
            "marked_chain_callable_tail_chain",
            "marked_chain_sig_ign_tail_skip",
            "marked_chain_none_tail_skip",
            "two_link_chain_sig_dfl_tail_defer_exit",
        ],
    )
    def test_classification_follows_effective_tail(self, build_original, expected):
        """Verdict equals the classification of the chain's effective tail."""
        assert _classify_signal_disposition(build_original()) is expected

    def test_classification_with_marker_cycle_terminates_as_chain(self):
        """A self-referential marker chain terminates (visited guard) as chain."""

        def _cyclic(signum, frame):
            pass

        _cyclic._baldur_chained_original = _cyclic

        assert _classify_signal_disposition(_cyclic) is _SignalDispositionMode.CHAIN


class TestRegisterSignalsDispositionBehavior:
    """register_signals disposition-sensitive install/skip behavior (597 D2)."""

    def _register_with_dispositions(self, coordinator, dispositions):
        """Run register_signals against a fake per-signal pre-disposition map.

        Returns the signal.signal mock so tests can assert on installs.
        No real OS handler is ever installed (xdist safety).
        """
        with (
            patch(
                "baldur.core.process_utils.is_under_gunicorn",
                return_value=False,
            ),
            patch("signal.getsignal", side_effect=lambda sig: dispositions[sig]),
            patch("signal.signal") as mock_signal,
        ):
            coordinator.register_signals()
        return mock_signal

    def _installed_handlers(self, mock_signal):
        """Map signum → installed handler from the signal.signal mock."""
        return {args[0][0]: args[0][1] for args in mock_signal.call_args_list}

    def test_sig_ign_disposition_skips_registration(self, coordinator):
        """SIG_IGN pre-disposition → no handler installed (ignore intent kept)."""
        mock_signal = self._register_with_dispositions(
            coordinator,
            {signal.SIGTERM: signal.SIG_IGN, signal.SIGINT: signal.SIG_IGN},
        )

        mock_signal.assert_not_called()

    def test_c_level_none_disposition_skips_registration(self, coordinator):
        """None (C-level handler, unknowable owner) → no handler installed."""
        mock_signal = self._register_with_dispositions(
            coordinator,
            {signal.SIGTERM: None, signal.SIGINT: None},
        )

        mock_signal.assert_not_called()

    def test_callable_disposition_installs_handler_with_chain_markers(
        self, coordinator
    ):
        """Callable original → chaining handler carrying both markers installed."""

        # Given — distinct host-server-style handlers per signal
        def _host_sigterm(signum, frame):
            pass

        def _host_sigint(signum, frame):
            pass

        originals = {signal.SIGTERM: _host_sigterm, signal.SIGINT: _host_sigint}

        # When
        mock_signal = self._register_with_dispositions(coordinator, originals)

        # Then — each installed handler exposes its captured original and owner
        installed = self._installed_handlers(mock_signal)
        assert set(installed) == {signal.SIGTERM, signal.SIGINT}
        for sig, handler in installed.items():
            assert handler._baldur_chained_original is originals[sig]
            assert handler._baldur_coordinator is coordinator

    def test_sig_dfl_disposition_installs_defer_exit_handler(self, coordinator):
        """SIG_DFL original → handler installed in defer-exit mode (arms signum)."""
        # Given
        mock_signal = self._register_with_dispositions(
            coordinator,
            {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_DFL},
        )
        handler = self._installed_handlers(mock_signal)[signal.SIGTERM]

        # When — first delivery with the drain suppressed (mode probe only)
        with patch.object(coordinator, "initiate_shutdown"):
            handler(signal.SIGTERM, None)

        # Then — defer-exit mode armed the exit signum
        assert coordinator._exit_signum == signal.SIGTERM

    def test_second_registration_with_own_handler_installed_is_noop(self, coordinator):
        """Re-registration guard: coordinator's own handler found → no install."""
        own_handler = coordinator._make_signal_handler(signal.SIG_DFL, defer_exit=True)

        mock_signal = self._register_with_dispositions(
            coordinator,
            {signal.SIGTERM: own_handler, signal.SIGINT: own_handler},
        )

        mock_signal.assert_not_called()

    def test_own_handler_buried_in_chain_is_noop(self, coordinator):
        """Re-registration guard walks markers: own handler deeper in the chain."""
        own_handler = coordinator._make_signal_handler(signal.SIG_DFL, defer_exit=True)
        wrapped = _marked_chain_handler(own_handler)

        mock_signal = self._register_with_dispositions(
            coordinator,
            {signal.SIGTERM: wrapped, signal.SIGINT: wrapped},
        )

        mock_signal.assert_not_called()

    def test_premarked_buffer_chain_with_sig_dfl_tail_installs_defer_exit(
        self, coordinator
    ):
        """Order-invariance: buffer chain over SIG_DFL still verdicts defer-exit."""
        # Given — a disk_buffer-style chained closure registered BEFORE us
        buffer_chain = _marked_chain_handler(signal.SIG_DFL)
        mock_signal = self._register_with_dispositions(
            coordinator,
            {signal.SIGTERM: buffer_chain, signal.SIGINT: buffer_chain},
        )
        handler = self._installed_handlers(mock_signal)[signal.SIGTERM]

        # When
        with patch.object(coordinator, "initiate_shutdown"):
            handler(signal.SIGTERM, None)

        # Then — callable head did not flip the SIG_DFL-tail verdict
        assert coordinator._exit_signum == signal.SIGTERM

    def test_premarked_buffer_chain_with_callable_tail_installs_chain_mode(
        self, coordinator
    ):
        """Order-invariance: buffer chain over a host handler verdicts chain."""
        # Given — buffer closure chained over a host-server handler
        chain_invocations = []

        def _buffer_head(signum, frame):
            chain_invocations.append((signum, frame))

        _buffer_head._baldur_chained_original = lambda signum, frame: None

        mock_signal = self._register_with_dispositions(
            coordinator,
            {signal.SIGTERM: _buffer_head, signal.SIGINT: _buffer_head},
        )
        handler = self._installed_handlers(mock_signal)[signal.SIGTERM]

        # When — first delivery chains the captured head (not the tail)
        with patch.object(coordinator, "initiate_shutdown"):
            handler(signal.SIGTERM, None)

        # Then — chain mode: head invoked, no exit signum armed
        assert chain_invocations == [(signal.SIGTERM, None)]
        assert coordinator._exit_signum is None


class TestCoordinatorSignalHandlerDispatchBehavior:
    """Installed handler first/subsequent dispatch via the TAS (597 D3)."""

    def _capture_handler(self, coordinator, original, sig=signal.SIGTERM):
        """Register against a fake disposition and capture the installed handler."""
        with (
            patch(
                "baldur.core.process_utils.is_under_gunicorn",
                return_value=False,
            ),
            patch("signal.getsignal", return_value=original),
            patch("signal.signal") as mock_signal,
        ):
            coordinator.register_signals()
        for args in mock_signal.call_args_list:
            if args[0][0] == sig:
                return args[0][1]
        raise AssertionError("coordinator handler was not installed")

    def test_first_delivery_in_defer_mode_arms_signum_and_initiates_drain(
        self, coordinator
    ):
        """First defer-mode delivery arms the exit signum and starts the drain."""
        handler = self._capture_handler(coordinator, signal.SIG_DFL)

        # Drain-thread exit seam MUST be patched — an unmocked invocation
        # kills the test process (597 Testability Notes).
        with patch.object(coordinator, "_arm_deferred_exit", autospec=True):
            handler(signal.SIGTERM, None)

            assert coordinator._exit_signum == signal.SIGTERM
            assert coordinator.phase != ShutdownPhase.RUNNING
            coordinator.wait_for_shutdown(timeout=2.0)

    def test_first_delivery_in_chain_mode_initiates_drain_before_original(
        self, coordinator
    ):
        """Chain mode: drain initiated FIRST, then the captured handler runs."""
        # Given — original records the coordinator phase at invocation time
        observed = []

        def _original(signum, frame):
            observed.append((signum, frame, coordinator.phase))

        handler = self._capture_handler(coordinator, _original)

        # When
        with patch.object(coordinator, "_arm_deferred_exit", autospec=True):
            handler(signal.SIGTERM, None)
            coordinator.wait_for_shutdown(timeout=2.0)

        # Then — original saw a post-initiate phase; no exit signum in chain mode
        assert len(observed) == 1
        signum, frame, phase_at_chain = observed[0]
        assert (signum, frame) == (signal.SIGTERM, None)
        assert phase_at_chain != ShutdownPhase.RUNNING
        assert coordinator._exit_signum is None

    def test_subsequent_delivery_in_defer_mode_performs_exit_reraise(self, coordinator):
        """Second defer-mode delivery (operator escape / trampoline landing)
        restores + re-raises without re-entering initiate_shutdown."""
        handler = self._capture_handler(coordinator, signal.SIG_DFL)

        with (
            patch.object(coordinator, "_arm_deferred_exit", autospec=True),
            patch.object(coordinator, "_perform_exit_reraise", autospec=True) as m_exit,
            patch.object(
                coordinator,
                "initiate_shutdown",
                wraps=coordinator.initiate_shutdown,
            ) as m_initiate,
        ):
            # When — nested/double delivery simulated by direct double invocation
            handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)
            coordinator.wait_for_shutdown(timeout=2.0)

        # Then — TAS dispatch: one initiate, one exit re-raise
        m_initiate.assert_called_once()
        m_exit.assert_called_once_with(signal.SIGTERM)

    def test_subsequent_delivery_in_chain_mode_forwards_to_original(self, coordinator):
        """Chain mode forwards subsequent deliveries so the host owner keeps
        its own double-signal semantics (uvicorn second-SIGINT force_exit)."""
        # Given
        forwarded = []

        def _original(signum, frame):
            forwarded.append((signum, frame))

        handler = self._capture_handler(coordinator, _original)

        # When
        with (
            patch.object(coordinator, "_arm_deferred_exit", autospec=True),
            patch.object(coordinator, "_perform_exit_reraise", autospec=True) as m_exit,
        ):
            handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)
            coordinator.wait_for_shutdown(timeout=2.0)

        # Then — both deliveries reached the host handler; no exit re-raise
        assert forwarded == [(signal.SIGTERM, None), (signal.SIGTERM, None)]
        m_exit.assert_not_called()


class TestDeferredExitArmingBehavior:
    """_arm_deferred_exit drain-thread exit seam (597 D4)."""

    def test_arm_deferred_exit_without_armed_signum_is_noop(self, coordinator):
        """Manual initiate_shutdown never exits: signum None → no kill/_exit."""
        with (
            patch("baldur.core.shutdown_coordinator.os.kill") as mock_kill,
            patch("baldur.core.shutdown_coordinator.time.sleep") as mock_sleep,
            patch("baldur.core.shutdown_coordinator.os._exit") as mock_exit,
        ):
            coordinator._arm_deferred_exit()

        mock_kill.assert_not_called()
        mock_sleep.assert_not_called()
        mock_exit.assert_not_called()

    def test_arm_deferred_exit_runs_kill_sleep_deadman_exit_in_order(self, coordinator):
        """Two-hop trampoline order: self-kill → deadman grace → os._exit(128+n)."""
        # Given
        coordinator._exit_signum = signal.SIGTERM
        manager = MagicMock()

        # When
        with (
            patch("baldur.core.shutdown_coordinator.os.kill", new=manager.kill),
            patch("baldur.core.shutdown_coordinator.time.sleep", new=manager.sleep),
            patch("baldur.core.shutdown_coordinator.os._exit", new=manager.exit),
        ):
            coordinator._arm_deferred_exit()

        # Then — exact order and arguments of the trampoline + deadman
        assert manager.mock_calls == [
            call.kill(os.getpid(), signal.SIGTERM),
            call.sleep(_SIGNAL_EXIT_DEADMAN_SECONDS),
            call.exit(128 + signal.SIGTERM),
        ]

    def test_arm_deferred_exit_deadman_code_tracks_armed_signum(self, coordinator):
        """Deadman exit code is 128 + the armed signum (SIGINT variant)."""
        coordinator._exit_signum = signal.SIGINT

        with (
            patch("baldur.core.shutdown_coordinator.os.kill"),
            patch("baldur.core.shutdown_coordinator.time.sleep"),
            patch("baldur.core.shutdown_coordinator.os._exit") as mock_exit,
        ):
            coordinator._arm_deferred_exit()

        mock_exit.assert_called_once_with(128 + signal.SIGINT)


class TestExitReraiseBehavior:
    """_perform_exit_reraise main-thread exit seam (597 D4)."""

    def test_perform_exit_reraise_restores_sig_dfl_then_redelivers(self, coordinator):
        """Restore SIG_DFL FIRST, then self-deliver → true signal death."""
        manager = MagicMock()

        with (
            patch(
                "baldur.core.shutdown_coordinator.signal.signal",
                new=manager.restore,
            ),
            patch("baldur.core.shutdown_coordinator.os.kill", new=manager.kill),
        ):
            coordinator._perform_exit_reraise(signal.SIGTERM)

        assert manager.mock_calls == [
            call.restore(signal.SIGTERM, signal.SIG_DFL),
            call.kill(os.getpid(), signal.SIGTERM),
        ]


class TestDrainExitParityBehavior:
    """Both TERMINATED paths invoke the exit arming seam exactly once (597 D10)."""

    def test_clean_drain_path_invokes_exit_arming_seam_once(
        self, request_tracker, shutdown_handler
    ):
        """Clean drain (TERMINATED via in_flight_drained) arms the exit."""
        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            drain_timeout=5.0,
            shutdown_handler=shutdown_handler,
            check_interval=0.05,
        )

        with patch.object(coordinator, "_arm_deferred_exit", autospec=True) as m_arm:
            coordinator.initiate_shutdown()
            assert coordinator.wait_for_shutdown(timeout=3.0) is True

        assert coordinator.phase == ShutdownPhase.TERMINATED
        m_arm.assert_called_once_with()

    def test_forced_drain_path_invokes_exit_arming_seam_once(
        self, request_tracker, shutdown_handler
    ):
        """Forced drain (TERMINATING → TERMINATED) arms the exit identically."""
        request_tracker.start_request("stuck-1")
        coordinator = GracefulShutdownCoordinator(
            request_tracker=request_tracker,
            drain_timeout=0.2,
            shutdown_handler=shutdown_handler,
            check_interval=0.05,
        )

        with patch.object(coordinator, "_arm_deferred_exit", autospec=True) as m_arm:
            coordinator.initiate_shutdown()
            coordinator.wait_for_shutdown(timeout=3.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        m_arm.assert_called_once_with()
